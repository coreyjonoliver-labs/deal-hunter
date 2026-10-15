"""Run realistic listing text through ai-marketplace-monitor's own matcher."""

from __future__ import annotations

import pytest
from ai_marketplace_monitor.utils import is_substring

from deal_hunter import profile

# What a mini-pc watch drops on.
ANTIKEYWORDS = profile.COMMON_ANTIKEYWORDS + profile.MINI_PC_ANTIKEYWORDS

KEEP = [
    "Intel NUC8i5BEH 16GB RAM 256GB SSD",
    "Beelink SER5 Pro Ryzen 7 5800H mini pc",
    "Minisforum UM790 Pro 32GB/1TB",
    "GMKtec NucBox G3 N100",
    "ASUS NUC 14 Pro barebone",
    "Geekom IT13 i9 Mini-PC",
    "Small desktop Mini PC with power adapter, works great",
    "Trigkey Green G4 N100 16GB 500GB",
]

GATED_OUT = [
    "Dell 27 inch monitor",
    "Gaming PC RTX 3080 tower",
    "MacBook Pro 2019",
]

ANTI = [
    "Intel NUC for parts",
    "Beelink mini pc - not working, no power",
    "NUC7i3 BIOS locked, sold as is",
    "Want to buy a mini pc under $100",
    "Beelink EQ12 doesn’t work",
    "WTB Intel NUC",
    "Minisforum case only, no board",
    "NUC power supply only",
    "Beelink EQ12 N100 - shipping only, no local pickup",
]


@pytest.mark.parametrize("title", KEEP)
def test_good_listings_pass_keywords_and_antikeywords(title: str) -> None:
    assert is_substring(profile.NUC_KEYWORDS, title)
    assert not is_substring(ANTIKEYWORDS, title)


@pytest.mark.parametrize("title", GATED_OUT)
def test_non_mini_pcs_fail_keyword_gate(title: str) -> None:
    assert not is_substring(profile.NUC_KEYWORDS, title)


# Healthy listings whose wording tripped earlier, too-broad antikeywords.
NOT_ANTI = [
    "Intel NUC11, never broken or bricked, works perfect",
    "Looking for a new home for my Beelink SER5",
    "Beelink EQ12, no power issues, includes adapter",
    "NUC pickup only, dealership only hours on weekends",
]


@pytest.mark.parametrize("title", NOT_ANTI)
def test_healthy_listings_are_not_dropped(title: str) -> None:
    assert not is_substring(ANTIKEYWORDS, title)


@pytest.mark.parametrize("title", ANTI)
def test_junk_listings_hit_antikeywords(title: str) -> None:
    assert is_substring(ANTIKEYWORDS, title)


def test_barebones_antikeywords_catch_barebone_listings() -> None:
    assert is_substring(profile.BAREBONES_ANTIKEYWORDS, "ASUS NUC 14 Pro barebone kit")
    assert is_substring(profile.BAREBONES_ANTIKEYWORDS, "Beelink EQ12, no RAM no SSD")
    assert not is_substring(profile.BAREBONES_ANTIKEYWORDS, "Beelink EQ12 16GB 500GB")


@pytest.mark.parametrize(
    "title",
    ["Lenovo ThinkCentre M720q Tiny", "Dell OptiPlex 7070 Micro", "HP EliteDesk 800 G4 Mini"],
)
def test_tiny_pc_keywords(title: str) -> None:
    assert is_substring(profile.TINY_KEYWORDS, title)


def test_every_keyword_parses_as_an_expression() -> None:
    # A keyword that fails to parse silently becomes a literal substring in
    # aimm, which would make quoted phrases match their quote characters.
    from ai_marketplace_monitor.utils import expr

    for kw in (
        profile.NUC_KEYWORDS + profile.TINY_KEYWORDS + ANTIKEYWORDS + profile.BAREBONES_ANTIKEYWORDS
    ):
        expr.parseString(kw, parseAll=True)


def test_rating_prompts_keep_aimm_parse_format() -> None:
    assert '"Rating <1-5>: <summary>"' in profile.MINI_PC_RATING_PROMPT
    assert '"Rating <1-5>: <summary>"' in profile.GENERIC_RATING_PROMPT


@pytest.mark.parametrize(
    "answer",
    [
        "The listing is a NUC11.\nRating 4: NUC11 i5, 16GB/512GB, fair ~$220 vs $150 ask, buy",
        "Rating 5: Beelink SER5 Ryzen 5 5560U 16GB/500GB, fair ~$200 vs $120, buy",
    ],
)
def test_expected_answer_shape_parses_with_aimm_regex(answer: str) -> None:
    import re

    # Mirrors ai_marketplace_monitor.ai's rating extraction.
    assert re.search(r"Rating[^1-5]*[1-5]", answer, re.DOTALL)
    line = next(x for x in answer.split("\n") if "Rating" in x)
    matched = re.match(r".*Rating[^1-5]*([1-5])[:\s]*(.*)", line)
    assert matched and matched.group(1) in "45" and "fair" in matched.group(2)
