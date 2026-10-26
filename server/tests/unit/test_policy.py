import pytest

from app.policy import LABELS, MalformedResult, decide, validate_result


def full(**scores):
    return {label: scores.get(label, 0.0) for label in LABELS}


@pytest.mark.parametrize("label", ["minor", "sexual_violence", "bestiality", "self_harm", "real_person"])
@pytest.mark.parametrize("verified", [True, False])
def test_prohibited_at_08_blocks_everywhere(label, verified):
    d = decide([full(**{label: 0.8})], verified)
    assert d.status == "blocked"
    assert label in d.reasons[0]


@pytest.mark.parametrize("score", [0.5, 0.79])
def test_prohibited_between_05_and_08_is_held(score):
    assert decide([full(minor=score)], True).status == "held"


def test_below_05_is_published():
    assert decide([full(minor=0.49, explicit=0.3)], False).status == "published"


def test_explicit_published_only_when_verified():
    assert decide([full(explicit=0.95)], True).status == "published"
    d = decide([full(explicit=0.95)], False)
    assert d.status == "blocked" and d.explicit


def test_block_wins_over_hold_and_explicit():
    d = decide([full(explicit=0.9), full(self_harm=0.6), full(bestiality=0.81)], True)
    assert d.status == "blocked"


def test_every_frame_counts_last_frame_explicit():
    frames = [full() for _ in range(60)] + [full(explicit=0.9)]
    assert decide(frames, False).status == "blocked"


def test_borderline_explicit_in_unverified_is_held():
    assert decide([full(explicit=0.6)], False).status == "held"
    assert decide([full(explicit=0.6)], True).status == "published"


def test_validate_rejects_short_result():
    # A provider that answered for fewer frames than the video has must not count as a pass (rule 9)
    with pytest.raises(MalformedResult):
        validate_result([full()] * 8, 60)


@pytest.mark.parametrize("bad", [None, "x", [{"explicit": "lots"}], [{"minor": 0.1}], [full(minor=1.5)]])
def test_validate_rejects_garbage(bad):
    with pytest.raises(MalformedResult):
        validate_result(bad, 1)
