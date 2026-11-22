"""The browsable promo deck stays a 24-slide page and keeps the held-draft caption."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DECK = ROOT / "docs" / "deck" / "index.html"
FRAMES = ROOT / "docs" / "deck" / "frames"


def test_deck_is_twenty_four_slides_with_the_public_repo():
    text = DECK.read_text(encoding="utf-8")
    assert text.count('<section class="slide"') == 24
    assert "github.com/sw30labs/atlas-workbench" in text
    assert "workbench-atlas" not in text
    assert "/" + "Users" + "/" not in text
    assert "It was not sent and not approved" in text
    assert "Send 0 to Review stays disabled" in text
    assert "PageDown" in text


def test_deck_frames_are_the_reviewed_copies():
    text = DECK.read_text(encoding="utf-8")
    for name in (
        "01-briefing.png",
        "02-constellation.png",
        "05-library.png",
        "06-brief.png",
        "08-signals.png",
        "10-review.png",
        "11-analyze.png",
        "12-organization.png",
        "14-add-research.png",
        "14b-preparing.png",
        "14c-draft-open.png",
        "15b-export-preview.png",
    ):
        assert f"frames/{name}" in text
        assert (FRAMES / name).is_file()
