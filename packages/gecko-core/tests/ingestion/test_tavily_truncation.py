from gecko_core.ingestion.discovery import _truncate_for_tavily


def test_short_passes_through():
    assert _truncate_for_tavily("hello") == "hello"


def test_at_boundary():
    s = "x" * 380
    assert _truncate_for_tavily(s) == s


def test_truncates_at_word_boundary():
    s = "word " * 100  # 500 chars
    out = _truncate_for_tavily(s)
    assert len(out) <= 380
    # Word-boundary: should end with "word" not "wor"
    assert not out.endswith("wor")


def test_truncates_hard_when_no_space():
    s = "x" * 500
    out = _truncate_for_tavily(s)
    assert len(out) == 380


def test_long_pro_idea():
    idea = "A pre-code planning CLI for indie developers about to build a new app. " * 10
    out = _truncate_for_tavily(idea)
    assert len(out) <= 380
