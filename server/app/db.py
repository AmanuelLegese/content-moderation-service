from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id text PRIMARY KEY,
    verified boolean NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    role text NOT NULL CHECK (role IN ('admin', 'member'))
);

CREATE TABLE IF NOT EXISTS media (
    id uuid PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    created_by text NOT NULL REFERENCES users(id),
    type text NOT NULL,
    source text NOT NULL,
    prompt text,
    frames jsonb NOT NULL,
    frame_count int NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'published', 'held', 'blocked')),
    -- why it is held: 'review' (a human must look) or 'provider_unavailable' (no decision yet)
    hold_kind text,
    reasons jsonb NOT NULL DEFAULT '[]',
    explicit boolean,
    -- set only when a complete provider result for every frame was applied
    screened_at timestamptz,
    decided_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS media_ws_status ON media (workspace_id, status);

-- One row per media item: the single successful scoring we paid for.
CREATE TABLE IF NOT EXISTS provider_results (
    media_id uuid PRIMARY KEY REFERENCES media(id),
    request_id text,
    frames jsonb NOT NULL,
    frames_scored int NOT NULL,
    via text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jobs (
    id uuid PRIMARY KEY,
    media_id uuid NOT NULL UNIQUE REFERENCES media(id),
    workspace_id text NOT NULL,
    -- queued -> running -> (waiting_callback ->) done
    --                   \\-> queued (retry) | parked (gave up for now, waits for provider recovery)
    status text NOT NULL CHECK (status IN ('queued', 'running', 'waiting_callback', 'parked', 'done')),
    attempts int NOT NULL DEFAULT 0,        -- provider calls made, all rounds
    round_failures int NOT NULL DEFAULT 0,  -- transient failures in the current round
    malformed int NOT NULL DEFAULT 0,
    rate_limited int NOT NULL DEFAULT 0,
    rescreen_rounds int NOT NULL DEFAULT 0,
    run_after timestamptz NOT NULL DEFAULT now(),
    lease_token uuid,
    lease_until timestamptz,
    request_id text,
    callback_deadline timestamptz,
    parked_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    done_at timestamptz
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs (workspace_id, run_after) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status);

CREATE TABLE IF NOT EXISTS events (
    id bigserial PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT now(),
    media_id uuid,
    job_id uuid,
    attempt int,
    event text NOT NULL,
    data jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS events_media ON events (media_id, id);
"""

SEED = """
INSERT INTO workspaces (id, verified) VALUES ('ws_verified', true), ('ws_unverified', false)
ON CONFLICT (id) DO UPDATE SET verified = EXCLUDED.verified;
INSERT INTO users (id, workspace_id, role) VALUES
    ('admin_v', 'ws_verified', 'admin'), ('member_v', 'ws_verified', 'member'),
    ('admin_u', 'ws_unverified', 'admin'), ('member_u', 'ws_unverified', 'member')
ON CONFLICT (id) DO UPDATE SET workspace_id = EXCLUDED.workspace_id, role = EXCLUDED.role;
"""

pool = ConnectionPool(
    config.DATABASE_URL,
    min_size=1,
    max_size=20,
    kwargs={"row_factory": dict_row},
    open=False,
)


def open_pool() -> None:
    pool.open(wait=True, timeout=60)


def init_db() -> None:
    with pool.connection() as conn:
        # Serialise schema creation between api and worker starting together
        conn.execute("SELECT pg_advisory_xact_lock(424242)")
        conn.execute(SCHEMA)
        conn.execute(SEED)
