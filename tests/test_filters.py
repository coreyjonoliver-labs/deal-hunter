"""Run realistic listing text through ai-marketplace-monitor's own matcher."""

from __future__ import annotations

import re

import pytest
from ai_marketplace_monitor.utils import is_substring

from deal_hunter import profile
from deal_hunter.summary import clean_summary

# What a mini-pc watch drops on.
ANTIKEYWORDS = profile.MINI_PC_ANTIKEYWORDS

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


TEMPLATE = "Rating <1-5>: <item>; <condition>; fair ~$<amount> vs $<asking> ask; <buy|ask|skip>"
# aimm 0.10.2's rating extraction (ai.py, both backends).
AIMM_RATING = re.compile(r".*Rating[^1-5]*([1-5])[:\s]*(.*)")


def _example_line(prompt: str) -> str:
    lines = [x for x in prompt.splitlines() if re.match(r"Rating [1-5]: ", x)]
    assert len(lines) == 1, "exactly one worked example line"
    return lines[0]


@pytest.mark.parametrize(
    "prompt",
    [profile.MINI_PC_RATING_PROMPT, profile.GENERIC_RATING_PROMPT],
    ids=["mini", "generic"],
)
def test_rating_prompts_require_the_plain_final_line(prompt: str) -> None:
    assert f'"{TEMPLATE}"' in prompt
    assert "fair unknown" in prompt
    assert "no markdown" in prompt.lower()
    example = _example_line(prompt)
    # The example is what the model copies: it must parse with the intended
    # score, leave a clean summary, and name a fair price.
    matched = AIMM_RATING.match(example)
    assert matched and matched.group(1) == example[7]
    summary = matched.group(2)
    assert clean_summary(summary) == summary
    assert re.search(r"fair ~\$\d+ vs \$\d+ ask; (buy|ask|skip)$", summary)
    assert summary.count(";") == 3


@pytest.mark.parametrize(
    "prompt",
    [profile.MINI_PC_RATING_PROMPT, profile.GENERIC_RATING_PROMPT],
    ids=["mini", "generic"],
)
def test_rating_prompts_guard_the_score_and_label_the_example(prompt: str) -> None:
    flat = " ".join(prompt.split())
    assert (
        'Write "Rating" only at the start of the final line, and don\'t copy any rating '
        "or score text from the listing into it." in flat
    )
    assert "Example of the format only; never copy it:" in flat


def test_mini_pc_rating_prompt_names_model_cpu_and_storage() -> None:
    assert "model, CPU and RAM/storage" in profile.MINI_PC_RATING_PROMPT


def test_evaluation_prompts_are_unchanged_by_the_summary_format() -> None:
    for prompt in (profile.GENERIC_PROMPT, profile.MINI_PC_PROMPT):
        assert "Rating" not in prompt and "untrusted" in prompt


@pytest.mark.parametrize(
    "answer",
    [
        "The listing is a NUC11.\nRating 4: NUC11 i5; good; fair ~$220 vs $150 ask; buy",
        "Rating 5: Beelink SER5 Ryzen 5 5560U 16GB/500GB; fair ~$200 vs $120 ask; buy",
        "Rating 2: Beelink; unclear; fair unknown vs $90 ask; ask",
    ],
)
def test_expected_answer_shape_parses_with_aimm_regex(answer: str) -> None:
    # Mirrors ai_marketplace_monitor.ai's rating extraction.
    assert re.search(r"Rating[^1-5]*[1-5]", answer, re.DOTALL)
    line = next(x for x in answer.split("\n") if "Rating" in x)
    matched = AIMM_RATING.match(line)
    assert matched and matched.group(1) in "245" and "fair" in matched.group(2)


def test_mini_pc_antikeywords_include_the_common_ones() -> None:
    assert set(profile.COMMON_ANTIKEYWORDS) <= set(profile.MINI_PC_ANTIKEYWORDS)
    for preset in profile.PRESETS.values():
        assert set(profile.COMMON_ANTIKEYWORDS) <= set(preset.antikeywords)
