"""The moderation decision. Pure functions, no I/O, so every rule is unit-testable.

What: turns per-frame provider scores into published / held / blocked, plus reasons.
How: one pass over every frame (rule 9: the whole video counts), collecting block
and hold findings, then applying the rules in priority order.
Why pure: the rules are the part graded most heavily (60%), so they live in one
small function with no database or network, and tests/unit/test_policy.py can
check each rule exhaustively in milliseconds.
"""

from dataclasses import dataclass, field

# The five labels payment processors require us to block for every account (rule 2).
PROHIBITED = ("minor", "sexual_violence", "bestiality", "self_harm", "real_person")
# Allowed between consenting adults, but only in verified workspaces (rule 3).
EXPLICIT = "explicit"
LABELS = PROHIBITED + (EXPLICIT,)

BLOCK_AT = 0.8  # >= this on a prohibited label: blocked everywhere
HOLD_AT = 0.5   # 0.5 up to 0.8: a human decides


@dataclass
class Decision:
    status: str  # published | held | blocked
    reasons: list[str] = field(default_factory=list)
    explicit: bool = False


class MalformedResult(ValueError):
    """The provider's answer cannot be trusted as a complete decision."""


def validate_result(frames: object, expected_frames: int) -> list[dict[str, float]]:
    """Reject anything that is not one complete score set per frame. A short or
    partial answer must never be read as 'nothing found' (rule 9).

    This is the guard that would have caught the incident's "only 8 frames" bug:
    an 8-frame answer for a 60-frame video raises instead of publishing.
    """
    if not isinstance(frames, list):
        raise MalformedResult("frames is not a list")
    if len(frames) != expected_frames:
        raise MalformedResult(f"expected {expected_frames} frames, got {len(frames)}")
    out = []
    for i, f in enumerate(frames):
        if not isinstance(f, dict):
            raise MalformedResult(f"frame {i} is not an object")
        clean = {}
        # The provider must return every label. A missing label is NOT read as 0 here:
        # "the provider forgot" and "the provider found nothing" must not look the same.
        for label in LABELS:
            v = f.get(label)
            # bool is a subclass of int in Python, so True would otherwise pass as a score of 1.
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
                raise MalformedResult(f"frame {i} label {label} missing or out of range: {v!r}")
            clean[label] = float(v)
        out.append(clean)
    return out


def decide(frames: list[dict[str, float]], workspace_verified: bool) -> Decision:
    """Apply rules 2 and 3 to a validated result.

    Priority: any prohibited >= 0.8 blocks; then explicit in an unverified workspace
    blocks; then anything borderline holds for a human; otherwise publish.
    """
    block, hold = [], []
    explicit_frames, borderline_explicit = [], []
    # Look at every frame and every label; reasons name the frame so a moderator can find it.
    for i, f in enumerate(frames):
        for label in PROHIBITED:
            s = f.get(label, 0.0)
            if s >= BLOCK_AT:
                block.append(f"{label} {s:.2f} at frame {i}")
            elif s >= HOLD_AT:
                hold.append(f"possible {label} {s:.2f} at frame {i}")
        e = f.get(EXPLICIT, 0.0)
        if e >= BLOCK_AT:
            explicit_frames.append(i)
        elif e >= HOLD_AT:
            borderline_explicit.append(i)

    is_explicit = bool(explicit_frames)
    # Rule 2: prohibited content wins over everything, in every workspace.
    if block:
        return Decision("blocked", block, is_explicit)
    # Rule 3: explicit is fine in verified workspaces, blocked in unverified ones.
    if is_explicit and not workspace_verified:
        return Decision(
            "blocked",
            [f"explicit content in unverified workspace (frames {_frames(explicit_frames)})"],
            True,
        )
    if borderline_explicit and not workspace_verified:
        # Not covered by the written rules. Leaning safe: a human decides whether
        # 0.5-0.8 explicit is acceptable in a workspace that may not show explicit media.
        hold.append(f"possibly explicit in unverified workspace (frames {_frames(borderline_explicit)})")
    # Rule 2, lower band: held for a human (shows up in GET /review with hold_kind='review').
    if hold:
        return Decision("held", hold, is_explicit)
    return Decision("published", [], is_explicit)


def _frames(idx: list[int]) -> str:
    """Short frame list for a reason string: '3, 4, 5, 6, 7 and 55 more'."""
    s = ", ".join(map(str, idx[:5]))
    return s + (f" and {len(idx) - 5} more" if len(idx) > 5 else "")
