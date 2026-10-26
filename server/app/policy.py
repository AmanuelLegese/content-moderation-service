"""The moderation decision. Pure functions, no I/O, so every rule is unit-testable."""

from dataclasses import dataclass, field

PROHIBITED = ("minor", "sexual_violence", "bestiality", "self_harm", "real_person")
EXPLICIT = "explicit"
LABELS = PROHIBITED + (EXPLICIT,)

BLOCK_AT = 0.8
HOLD_AT = 0.5


@dataclass
class Decision:
    status: str  # published | held | blocked
    reasons: list[str] = field(default_factory=list)
    explicit: bool = False


class MalformedResult(ValueError):
    pass


def validate_result(frames: object, expected_frames: int) -> list[dict[str, float]]:
    """Reject anything that is not one complete score set per frame. A short or
    partial answer must never be read as 'nothing found' (rule 9)."""
    if not isinstance(frames, list):
        raise MalformedResult("frames is not a list")
    if len(frames) != expected_frames:
        raise MalformedResult(f"expected {expected_frames} frames, got {len(frames)}")
    out = []
    for i, f in enumerate(frames):
        if not isinstance(f, dict):
            raise MalformedResult(f"frame {i} is not an object")
        clean = {}
        for label in LABELS:
            v = f.get(label)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
                raise MalformedResult(f"frame {i} label {label} missing or out of range: {v!r}")
            clean[label] = float(v)
        out.append(clean)
    return out


def decide(frames: list[dict[str, float]], workspace_verified: bool) -> Decision:
    block, hold = [], []
    explicit_frames, borderline_explicit = [], []
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
    if block:
        return Decision("blocked", block, is_explicit)
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
    if hold:
        return Decision("held", hold, is_explicit)
    return Decision("published", [], is_explicit)


def _frames(idx: list[int]) -> str:
    s = ", ".join(map(str, idx[:5]))
    return s + (f" and {len(idx) - 5} more" if len(idx) > 5 else "")
