"""Database schema, seed data and the shared connection pool.

What: creates the tables on startup, seeds the two workspaces and four users the
brief requires, and exposes one psycopg connection pool used by api and worker.
How: idempotent SQL (CREATE ... IF NOT EXISTS, INSERT ... ON CONFLICT) run on
every start, so there is no separate migration step: `docker compose up` is enough.
Why Postgres for everything (including the job queue): a job claim, the stored
provider result and the media status change can then commit in one transaction,
so they can never disagree. See docs/TOOLS.md.
"""

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

SCHEMA = """
-- A workspace is the tenant boundary. `verified` decides whether explicit media may be published (rule 3).
CREATE TABLE IF NOT EXISTS workspaces (
    id text PRIMARY KEY,
    verified boolean NOT NULL
);

-- Callers identify themselves with X-User-Id / X-Workspace-Id; this table says whether that pair is real
-- and whether the user is an admin (only admins review, rule 6).
CREATE TABLE IF NOT EXISTS users (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    role text NOT NULL CHECK (role IN ('admin', 'member'))
);

-- One row per uploaded or generated item. `frames` is the JSON document from the brief
-- (per-frame label scores); `status` is what users see.
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
    -- the admin who approved or rejected it; once set, late provider answers no longer change it
    decided_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
-- Serves the review queue (held media of one workspace) and the media list.
CREATE INDEX IF NOT EXISTS media_ws_status ON media (workspace_id, status);

-- One row per media item: the single successful scoring we paid for.
-- The primary key is what makes rule 8 hold: a second answer for the same media
-- (duplicate callback, redelivered job) cannot insert a second row.
CREATE TABLE IF NOT EXISTS provider_results (
    media_id uuid PRIMARY KEY REFERENCES media(id),
    request_id text,
    frames jsonb NOT NULL,
    frames_scored int NOT NULL,
    via text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);

-- The queue. One job per media (UNIQUE media_id), claimed with FOR UPDATE SKIP LOCKED
-- and held with a lease (lease_token + lease_until) so two workers never run it at once.
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
    run_after timestamptz NOT NULL DEFAULT now(),  -- not claimable before this (retry backoff)
    lease_token uuid,                              -- who holds the job; every write checks it
    lease_until timestamptz,                       -- after this the sweeper hands it to another worker
    request_id text,                               -- provider's id for an async (202) request
    callback_deadline timestamptz,                 -- resubmit if no callback by then
    parked_at timestamptz,                         -- re-screen only after a provider success later than this
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    done_at timestamptz
);
-- Partial index: only queued jobs are looked up by the claim query, so only they are indexed.
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs (workspace_id, run_after) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status);

-- Append-only job log (see logs.py). Lets anyone replay one media item's history via the API.
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

# The fixtures the brief requires. ON CONFLICT DO UPDATE makes it safe to run on every start,
# and resets the seeded users/flags if someone changed them.
SEED = """
INSERT INTO workspaces (id, verified) VALUES ('ws_verified', true), ('ws_unverified', false)
ON CONFLICT (id) DO UPDATE SET verified = EXCLUDED.verified;
INSERT INTO users (id, workspace_id, role) VALUES
    ('admin_v', 'ws_verified', 'admin'), ('member_v', 'ws_verified', 'member'),
    ('admin_u', 'ws_unverified', 'admin'), ('member_u', 'ws_unverified', 'member')
ON CONFLICT (id) DO UPDATE SET workspace_id = EXCLUDED.workspace_id, role = EXCLUDED.role;
"""

# One pool per process. Rows come back as dicts (row["status"]) which keeps the SQL-heavy code readable.
# open=False: importing this module must not connect (unit tests import app.api without a database).
pool = ConnectionPool(
    config.DATABASE_URL,
    min_size=1,
    max_size=20,
    kwargs={"row_factory": dict_row},
    open=False,
)


def open_pool() -> None:
    """Connect, waiting up to 60s for Postgres to accept connections (container start order)."""
    pool.open(wait=True, timeout=60)


def init_db() -> None:
    """Create the schema and seed data. Safe to run from every api and worker process at once."""
    # `with pool.connection()` commits on success and rolls back on exception.
    with pool.connection() as conn:
        # Serialise schema creation between api and worker starting together
        # (two concurrent CREATE TABLE IF NOT EXISTS can still collide in Postgres).
        conn.execute("SELECT pg_advisory_xact_lock(424242)")
        conn.execute(SCHEMA)
        conn.execute(SEED)
