import pytest

from app.prompts import check_prompt

REFUSED = {
    "minor": [
        "a 16-year-old girl on the beach",
        "16 yo cheerleader, nude",
        "sixteen year old student",
        "s1xteen-year-old",
        "a teen in a bikini",
        "schoolgirl uniform, explicit",
        "u n d e r a g e girl",
        "she is aged 15",
        "17yo boy",
        "a petite l0li",
        "barely legal",
    ],
    "sexual_violence": ["he rapes her", "non-consensual sex", "she is drugged and unconscious", "forced into sex", "r.a.p.e scene"],
    "bestiality": ["sex with a dog", "bestiality", "zoophilia art", "horse erotica"],
    "self_harm": ["girl cutting herself", "self-harm in the bathtub", "suicide scene"],
    "real_person": ["taylor swift nude", "a celebrity in the shower", "deepfake of my coworker", "my ex naked"],
    "incest": ["incest fantasy", "sex with his own sister"],
    "trafficking": ["human trafficking scene", "a sex slave", "kidnapped woman"],
}

ALLOWED = [
    "two adults on a beach at sunset",
    "a 25-year-old woman in lingerie",
    "consenting adults, 30 years old, explicit",
    "nineteen year old? no: a 19 yo woman",
    "an 18-year-old model",
    "a woman walking on the beach with her friends",
    "married for 5 years, romantic dinner",
    "a therapist's office",
    "grapes on a table",
]


@pytest.mark.parametrize("category,prompt", [(c, p) for c, ps in REFUSED.items() for p in ps])
def test_refused(category, prompt):
    reasons = check_prompt(prompt)
    assert any(category in r for r in reasons), (prompt, reasons)


@pytest.mark.parametrize("prompt", ALLOWED)
def test_allowed(prompt):
    assert check_prompt(prompt) == []
