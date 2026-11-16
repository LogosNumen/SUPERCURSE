"""Checks for the pure logic. Run: .venv\\Scripts\\python test_supercurse.py"""
from supercurse import clean_text, parse_hotkey, pick_ocr_text, sentence_span


def test_clean_text():
    assert clean_text("## Hi **there** see [docs](https://x.io) or https://a.b/c?d=1") == "Hi there see docs or link"
    assert clean_text("- one\n- _two_\n> `code`") == "one two code"
    assert clean_text("snake_case_name stays") == "snake_case_name stays"
    assert clean_text("take ur time ￼") == "take ur time"


def test_sentence_span():
    text = "First one. Second one! Third?"
    assert text[slice(*sentence_span(text, 0))].strip() == "First one."
    assert text[slice(*sentence_span(text, 14))].strip() == "Second one!"
    assert text[slice(*sentence_span(text, len(text)))].strip() == "Third?"


def test_parse_hotkey():
    assert parse_hotkey("Ctrl+Alt+E") == (0x4000 | 0x2 | 0x1, ord("E"))
    assert parse_hotkey("shift+F5") == (0x4000 | 0x4, 0x74)


def words(y, *items):
    """OCR line at height y: words given as (text, left, right)."""
    return [(t, (l, y, r, y + 20)) for t, l, r in items]


def test_pick_ocr_text():
    lines = [
        words(0, ("Hello", 0, 50), ("world.", 60, 120)),
        words(28, ("Next", 0, 40), ("bit.", 50, 90)),      # same paragraph: gap 8px
        words(80, ("Other", 0, 50), ("para", 60, 100)),    # new paragraph: gap 32px
        words(14, ("Side", 400, 440), ("column", 450, 520)),  # another window's text, sorts between lines 1 and 2
    ]
    assert pick_ocr_text(lines, 70, 10, "word")[0] == "world."
    assert pick_ocr_text(lines, 70, 10, "line")[0] == "Hello world."
    assert pick_ocr_text(lines, 10, 35, "sentence") == ("Next bit.", (0, 28, 90, 48))
    assert pick_ocr_text(lines, 10, 35, "paragraph")[0] == "Hello world. Next bit."
    assert pick_ocr_text(lines, 10, 90, "paragraph")[0] == "Other para"
    assert pick_ocr_text([], 0, 0, "line") is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
