"""The allowlisted links a listing may be shown with."""

from __future__ import annotations

import pytest

from deal_hunter.links import cdn_image_url, item_url

NOW = 1_700_000_000.0


@pytest.mark.parametrize("item_id", ["123", "9" * 25])
def test_item_url(item_id: str) -> None:
    assert item_url(item_id) == f"https://www.facebook.com/marketplace/item/{item_id}/"


@pytest.mark.parametrize("item_id", ["", "123\n", "١٢٣", "1" * 26, "1/2", "a1"])
def test_item_url_is_ascii_digits_only(item_id: str) -> None:
    assert item_url(item_id) is None


@pytest.mark.parametrize(
    "image",
    [
        "https://[x",
        "https://scontent.xx.fbcdn.net／p.jpg",
        "https://／",
        "http://[::1",
        "https://scontent.xx.fbcdn.net:abc/p.jpg",
    ],
)
def test_unparseable_images_are_refused_not_raised(image: str) -> None:
    assert cdn_image_url(image, NOW) is None


def test_a_good_image_passes() -> None:
    url = "https://scontent.xx.fbcdn.net/v/p.jpg?oe=7FFFFFFF"
    assert cdn_image_url(url, NOW) == url
