"""The two kinds of link a listing may be shown with: its Marketplace page and its photo.

Both come from aimm's cache or from listing text, which a seller (or, after a
browser exploit, anything) controls, so each is rebuilt or checked against an
allowlist before the dashboard or a notification shows it.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_ITEM_ID = re.compile(r"[0-9]{1,25}")


def item_url(item_id: str) -> str | None:
    """The Marketplace page for a listing id, or None unless the id is all digits."""
    if _ITEM_ID.fullmatch(item_id):
        return f"https://www.facebook.com/marketplace/item/{item_id}/"
    return None


def cdn_image_url(image: str, now: float) -> str | None:
    """The photo, if it's on Facebook's CDN and its signed link hasn't expired."""
    if "\\" in image or "@" in image or any(c.isspace() for c in image):
        return None  # browsers and urlparse can disagree on the host
    try:
        parsed = urlparse(image)
        host = parsed.hostname or ""
        parsed.port  # noqa: B018 — raises ValueError for a bad port
    except ValueError:  # "https://[x", full-width slashes, ...
        return None
    if parsed.scheme != "https" or not (host == "fbcdn.net" or host.endswith(".fbcdn.net")):
        return None
    # oe= is the link's expiry, as hex Unix time; after it the CDN returns 403.
    expiry = parse_qs(parsed.query).get("oe", [""])[0]
    try:
        if int(expiry, 16) < now:
            return None
    except ValueError:
        pass
    return image
