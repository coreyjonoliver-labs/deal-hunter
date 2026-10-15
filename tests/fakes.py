"""Stand-ins for the Playwright objects deal-hunter's session code touches."""

from __future__ import annotations

from typing import Any

LOGIN_COOKIE = {"name": "c_user", "value": "100001", "domain": ".facebook.com", "path": "/"}


class FakeContext:
    def __init__(self, cookies: list[dict[str, Any]] | None = None) -> None:
        self.cookie_list = list(cookies or [])
        self.urls: list[Any] = []

    def cookies(self, urls: Any = None) -> list[dict[str, Any]]:
        self.urls.append(urls)
        return self.cookie_list

    def storage_state(self) -> dict[str, Any]:
        return {"cookies": self.cookie_list, "origins": []}


class FakeBrowserPage:
    def __init__(self, context: FakeContext, url: str = "about:blank") -> None:
        self.context = context
        self.url = url
        self.closed = False
        self.login_form = False  # a password box on the page

    def query_selector(self, selector: str) -> object | None:
        assert selector == 'input[name="pass"]'
        return object() if self.login_form else None

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, cookies: list[dict[str, Any]] | None = None) -> None:
        self.cookies = cookies
        self.context_options: list[dict[str, Any]] = []

    def new_context(self, **options: Any) -> Any:
        self.context_options.append(options)
        state = options.get("storage_state")
        cookies = state["cookies"] if state else []
        context = FakeContext(cookies)

        class _Ctx:
            def new_page(_self) -> FakeBrowserPage:
                return FakeBrowserPage(context)

        return _Ctx()
