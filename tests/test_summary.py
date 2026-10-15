"""Rating-summary cleaning: markdown debris out, the words and the price kept."""

from __future__ import annotations

import pytest

from deal_hunter.summary import MAX_LENGTH, clean_summary

GOOD = "Uplift V2 60x30; light wear; fair ~$350 vs $220 ask; buy"


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        # trailing / wrapping emphasis
        (
            "Rolltop desk, wrong product category—skip.**",
            "Rolltop desk, wrong product category—skip.",
        ),
        (f"**{GOOD}**", GOOD),
        (f"__{GOOD}__", GOOD),
        (f"*{GOOD}*", GOOD),
        (f"_{GOOD}_", GOOD),
        (f"`{GOOD}`", GOOD),
        ("***bold italic*** deal", "bold italic deal"),
        ("**bold _nested_ text** here", "bold nested text here"),
        (
            "Beelink *SER5* 16GB; fair ~$200 vs $120 ask; buy",
            "Beelink SER5 16GB; fair ~$200 vs $120 ask; buy",
        ),
        ("Use `code` here", "Use code here"),
        # line-start markers
        ("# Great desk; buy", "Great desk; buy"),
        ("### Great desk; buy", "Great desk; buy"),
        ("> Great desk; buy", "Great desk; buy"),
        # a repeated rating prefix
        (f"Rating 4: {GOOD}", GOOD),
        (f"**Rating 4:** {GOOD}", GOOD),
        (f"Rating 4: Rating 4: {GOOD}", GOOD),
        # whitespace
        ("Desk;\n\nfair   ~$90\tvs $60 ask;  buy  ", "Desk; fair ~$90 vs $60 ask; buy"),
        # untouched text
        (GOOD, GOOD),
        ("fair unknown vs $50 ask; ask", "fair unknown vs $50 ask; ask"),
        (
            "NUC11 i5, 16GB/512GB, fair ~$220 vs $150 ask, buy",
            "NUC11 i5, 16GB/512GB, fair ~$220 vs $150 ask, buy",
        ),
        (
            "snake_case_name 2*3 #1 pick, rating 4 stars",
            "snake_case_name 2*3 #1 pick, rating 4 stars",
        ),
        ("Dell 3 * 4 ports; skip", "Dell 3 * 4 ports; skip"),
        # a removed marker never joins the characters either side
        ("[x](ja**vascript:alert(1))", "[x](ja vascript:alert(1))"),
        ("<scr**ipt>", "<scr ipt>"),
        ("<a hr__ef=x>", "<a hr ef=x>"),
        ("[x]`(`http://e)", "[x] ( http://e)"),
        ("<*script>", "< script>"),
        # nothing left
        ("", ""),
        ("   ", ""),
        ("****", ""),
        ("**  **", ""),
        ("Rating 3:", ""),
    ],
)
def test_clean_summary(raw: str, cleaned: str) -> None:
    assert clean_summary(raw) == cleaned


def test_clean_summary_is_idempotent() -> None:
    once = clean_summary(f"**Rating 4: {GOOD}**")
    assert clean_summary(once) == once == GOOD


def test_long_summary_truncates_on_a_word_boundary() -> None:
    raw = " ".join(f"word{i:03d}" for i in range(60))  # 480 chars
    assert len(raw) > 400
    out = clean_summary(raw)
    assert len(out) <= MAX_LENGTH == 300
    assert out.endswith("…")
    assert out[:-1] == " ".join(raw.split()[: len(out[:-1].split())])  # whole words only
    assert not out[:-1].endswith(" ")


def test_exactly_at_the_limit_is_not_truncated() -> None:
    raw = "a" * 299 + "b"
    assert clean_summary(raw) == raw


def test_long_text_without_spaces_is_hard_cut() -> None:
    out = clean_summary("x" * 400)
    assert len(out) == MAX_LENGTH and out.endswith("…")


def test_truncation_does_not_leave_trailing_punctuation_before_the_ellipsis() -> None:
    raw = ("word; " * 80).strip()
    out = clean_summary(raw)
    assert len(out) <= MAX_LENGTH and out.endswith("word…")


@pytest.mark.parametrize(
    ("raw", "reassembled"),
    [
        ("[x](ja**vascript:alert(1))", "javascript:"),
        ("<scr**ipt>", "<script>"),
        ("<a hr__ef=x>", "href"),
        ("[x]`(`http://e)", "](http"),
        ("[x]`(`http://e)", "]("),
    ],
)
def test_cleaning_never_reassembles_markup(raw: str, reassembled: str) -> None:
    assert reassembled not in clean_summary(raw)


def test_cap_truncates_without_cleaning() -> None:
    from deal_hunter.summary import cap

    assert cap("****") == "****"
    out = cap("word " * 100)
    assert len(out) <= MAX_LENGTH and out.endswith("word…")
