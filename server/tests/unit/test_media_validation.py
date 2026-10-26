import pytest
from pydantic import ValidationError

from app.api import MediaIn


def test_brief_example_is_valid():
    m = MediaIn.model_validate({"type": "video", "source": "generation", "prompt": "two adults on a beach at sunset",
                                "frames": [{"explicit": 0.02}, {"explicit": 0.95, "self_harm": 0.1}]})
    assert len(m.frames) == 2


def test_source_defaults_to_upload():
    assert MediaIn.model_validate({"type": "image", "frames": [{}]}).source == "upload"


@pytest.mark.parametrize("item", [
    {"type": "image", "frames": [{}, {}]},                       # an image has one frame
    {"type": "video", "frames": []},
    {"type": "image", "frames": [{"minor": 1.2}]},
    {"type": "image", "frames": [{"minor": -0.1}]},
    {"type": "image", "source": "generation", "frames": [{}]},   # generated media carries its prompt
    {"type": "image", "source": "generation", "prompt": "  ", "frames": [{}]},
    {"type": "gif", "frames": [{}]},
    {"type": "image", "source": "scraped", "frames": [{}]},
])
def test_invalid(item):
    with pytest.raises(ValidationError):
        MediaIn.model_validate(item)
