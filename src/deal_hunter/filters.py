"""The monitor's listing filters, applied outside it (`check`, `web`)."""

from __future__ import annotations

import re
from typing import Any

from deal_hunter.launch import is_substring_without_urls as is_substring

_PRICE = re.compile(r"\$\s*([\d,]+)")


def parse_price(text: str) -> int | None:
    """The asking price from aimm's price text, e.g. "$60 | $100" → 60."""
    m = _PRICE.search(text)
    return int(m.group(1).replace(",", "")) if m else None


def drop_reasons(
    item: dict[str, Any],
    text: str,
    price: int | None = None,
    location: str | None = None,
    seller_locations: list[str] | None = None,
) -> list[str]:
    """Why the monitor would drop a listing for item (a rendered [item.*] table)."""
    reasons = []
    if not is_substring(item["keywords"], text):
        reasons.append("no brand/form-factor keyword")
    if is_substring(item["antikeywords"], text):
        hits = [k for k in item["antikeywords"] if is_substring(k, text)]
        reasons.append(f"antikeyword {', '.join(hits)}")
    if price is not None and not item["min_price"] <= price <= item["max_price"]:
        reasons.append(f"price outside {item['min_price']}-{item['max_price']}")
    if location is not None and seller_locations and not is_substring(seller_locations, location):
        reasons.append(f"not local ({location})")
    return reasons
