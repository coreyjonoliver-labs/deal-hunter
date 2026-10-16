"""`deal-hunter web`: joining aimm's cache, metrics, the page and the server."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from ai_marketplace_monitor.listing import Listing
from diskcache import Cache
from fakes import LOGIN_COOKIE, FakeContext

from deal_hunter import dashboard, session
from deal_hunter.events import EVENTS_NAME
from deal_hunter.settings import Settings, parse_settings

NOW = 1_800_000_000.0
FRESH_IMAGE = f"https://scontent.fcid1-1.fna.fbcdn.net/v/x.jpg?oe={int(NOW + 3600):X}&oh=1"
OLD_IMAGE = f"https://scontent.fcid1-1.fna.fbcdn.net/v/x.jpg?oe={int(NOW - 3600):X}"


def listing(id: str, title: str, location: str = "Springfield, IL", **kw: str) -> dict[str, str]:
    return {
        "marketplace": "facebook",
        "name": "",
        "id": id,
        "title": title,
        "image": kw.get("image", FRESH_IMAGE),
        "price": kw.get("price", "$150"),
        "post_url": f"https://www.facebook.com/marketplace/item/{id}/?ref=search",
        "location": location,
        "seller": "**unspecified**",
        "condition": "Used - Good",
        "description": kw.get("description", ""),
    }


def put(
    cache: Cache,
    details: dict[str, str],
    score: int | None,
    comment: str = "",
    watch: str = "nuc",
) -> None:
    cache.set(("listing-details", details["post_url"].split("?")[0]), details)
    if score is not None:
        hashed = Listing(**{**details, "name": watch}).hash
        cache.set(("ai-inquiries", "item", "market", hashed), {"score": score, "comment": comment})


@pytest.fixture
def settings(base_settings: dict[str, Any], tmp_path: Path) -> Settings:
    base_settings["location"]["seller_locations"] = ["Springfield", "Shelbyville"]
    base_settings["facebook"] = {"session_file": str(tmp_path / "fb.json")}
    return parse_settings(base_settings)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "aimm"
    home.mkdir()
    with Cache(str(home)) as cache:
        put(cache, listing("1", "Beelink SER5 Ryzen 5 16GB"), 5, "Great deal")
        put(cache, listing("2", "Intel NUC11 i5", "Shelbyville, IL"), 2, "Meh")
        put(cache, listing("3", "Minisforum UM790", "Bloomington, IL"), 4, "Far away")
        # Stored before summaries were cleaned at write time.
        put(cache, listing("4", "Toro string trimmer"), 1, "**Not a PC—skip.**")
        put(cache, listing("5", "GMKtec NucBox G3", image=OLD_IMAGE), None)
        cache.set(("user-notifications", "facebook", "1", "me"), NOW)
        cache.set(("counters", "Search performed", "nuc"), 12)
    return home


def events(home: Path, *items: dict[str, Any]) -> None:
    with (home / EVENTS_NAME).open("a") as f:
        for item in items:
            f.write(json.dumps(item) + "\n")


def test_collect_joins_ratings_and_todays_filters(settings: Settings, home: Path) -> None:
    snap = dashboard.collect(settings, home, NOW)
    by_id = {r.id: r for r in snap.results}
    assert by_id["1"].score == 5 and by_id["1"].notified and by_id["1"].comment == "Great deal"
    assert by_id["2"].score == 2 and not by_id["2"].notified
    assert by_id["3"].dropped == ["not local (Bloomington, IL)"]
    assert by_id["4"].dropped == ["no required keyword"]
    assert by_id["5"].score is None and not by_id["5"].dropped
    assert snap.counters == {"Search performed": 12}


def test_old_cached_comments_are_cleaned_on_display(settings: Settings, home: Path) -> None:
    by_id = {r.id: r for r in dashboard.collect(settings, home, NOW).results}
    assert by_id["4"].comment == "Not a PC—skip."
    assert by_id["1"].comment == "Great deal"


def test_all_markdown_and_overlong_stored_comments_stay_capped(
    settings: Settings, tmp_path: Path
) -> None:
    home = tmp_path / "aimm2"
    home.mkdir()
    with Cache(str(home)) as cache:
        put(cache, listing("1", "Beelink SER5 Ryzen 5 16GB"), 5, "****")
        put(cache, listing("2", "Intel NUC11 i5", "Shelbyville, IL"), 3, "*" * 1000)
    by_id = {r.id: r for r in dashboard.collect(settings, home, NOW).results}
    assert by_id["1"].comment == "****"  # nothing else to show: kept as stored
    assert len(by_id["2"].comment) <= 300 and by_id["2"].comment.endswith("…")


def test_views(settings: Settings, home: Path) -> None:
    results = dashboard.collect(settings, home, NOW).results
    ids = lambda view: [r.id for r in dashboard.select(results, view, settings)]  # noqa: E731
    assert ids("matches") == ["1"]
    assert ids("rated") == ["1", "2"]
    assert ids("all")[:3] == ["1", "2", "5"]  # filtered-out ones last
    assert set(ids("all")) == {"1", "2", "3", "4", "5"}


def test_empty_home(settings: Settings, tmp_path: Path) -> None:
    snap = dashboard.collect(settings, tmp_path, NOW)
    assert snap.results == [] and snap.events == []
    page = dashboard.render_page(snap, settings)
    assert "Nothing here yet" in page and "No activity yet" in page


def test_unreadable_cache_is_reported(settings: Settings, tmp_path: Path) -> None:
    (tmp_path / "cache.db").write_text("not sqlite")
    snap = dashboard.collect(settings, tmp_path, NOW)
    assert snap.errors and "cache" in snap.errors[0]
    assert "banner" in dashboard.render_page(snap, settings)


@pytest.mark.parametrize(
    ("timeline", "label"),
    [
        ([], "No activity yet"),
        ([("browser_ready", NOW - 60)], "Starting"),
        ([("browser_ready", NOW - 3600)], "Starting"),  # a long first run is fine
        ([("browser_ready", NOW - 3 * 3600)], "Stalled"),
        ([("browser_ready", NOW - 7200), ("search_summary", NOW - 3600)], "Running"),
        (
            [("browser_ready", NOW - 9 * 3600), ("search_summary", NOW - 3 * 3600)],
            "Stalled or stopped",
        ),
    ],
)
def test_status(
    settings: Settings, home: Path, timeline: list[tuple[str, float]], label: str
) -> None:
    events(home, *({"ts": ts, "kind": kind} for kind, ts in timeline))
    snap = dashboard.collect(settings, home, NOW)
    assert dashboard.status(snap, settings).label == label


def test_ai_spend(settings: Settings, home: Path) -> None:
    usage = {"kind": "ai_usage", "model": "claude-haiku-4-5-20251001"}
    events(
        home,
        {**usage, "ts": NOW - 60, "input_tokens": 1_000_000, "output_tokens": 100_000},
        {**usage, "ts": NOW - 3 * 86_400, "input_tokens": 500_000, "output_tokens": 0},
        {**usage, "ts": NOW - 30 * 86_400, "input_tokens": 1_000_000, "output_tokens": 0},
    )
    ai = dashboard.summary(dashboard.collect(settings, home, NOW), settings)["ai"]
    assert ai["cost_24h"] == pytest.approx(1.5)
    assert ai["cost_7d"] == pytest.approx(2.0)
    assert ai["cost_total"] == pytest.approx(3.0)
    assert ai["calls_total"] == 3 and ai["input_tokens"] == 2_500_000


def test_unknown_model_cost_is_unknown() -> None:
    assert dashboard.usage_cost({"model": "mystery", "input_tokens": 5}) is None
    cached = {"model": "claude-haiku-4-5", "cache_read_tokens": 1_000_000}
    assert dashboard.usage_cost(cached) == pytest.approx(0.1)


def test_page_escapes_seller_text(settings: Settings, tmp_path: Path) -> None:
    with Cache(str(tmp_path)) as cache:
        evil = listing("7", 'Beelink <script>alert(1)</script> "x', description="")
        put(cache, evil, 5, "<img src=x onerror=alert(1)>")
    page = dashboard.render_page(dashboard.collect(settings, tmp_path, NOW), settings)
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    assert "<img src=x" not in page
    assert 'href="https://www.facebook.com/marketplace/item/7/"' in page


def test_links_and_photos_are_checked(settings: Settings, tmp_path: Path) -> None:
    with Cache(str(tmp_path)) as cache:
        put(cache, listing("../../evil", "Beelink A", image="https://evil.example/x.jpg"), 5)
        put(cache, listing("8", "Beelink B", image=OLD_IMAGE), 5)
        put(cache, listing("9", "Beelink C"), 5)
    results = {r.id: r for r in dashboard.collect(settings, tmp_path, NOW).results}
    assert results["../../evil"].url is None
    assert results["../../evil"].image_url(NOW) is None
    assert results["8"].image_url(NOW) is None  # expired CDN link
    assert results["9"].image_url(NOW) == FRESH_IMAGE


def test_session_info_never_reads_the_session(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert dashboard.session_info(settings, NOW)["problem"].startswith("No session details")
    cookie = {**LOGIN_COOKIE, "expires": NOW + 86_400}
    session.save(FakeContext([cookie]), settings.session_file)
    monkeypatch.setattr(session, "load", pytest.fail)  # metadata only
    info = dashboard.session_info(settings, NOW)
    assert info["expires"] == NOW + 86_400 and info["problem"] is None
    assert LOGIN_COOKIE["value"] not in json.dumps(info)
    assert dashboard.session_info(settings, NOW + 2 * 86_400)["problem"].startswith("The login")


def test_anonymous_session_info(base_settings: dict[str, Any]) -> None:
    base_settings["facebook"] = {"anonymous": True}
    assert dashboard.session_info(parse_settings(base_settings), NOW) == {"mode": "anonymous"}


@pytest.mark.parametrize(
    ("header", "host"),
    [
        ("127.0.0.1:8740", "127.0.0.1"),
        ("LOCALHOST", "localhost"),
        ("[::1]:8740", "::1"),
        ("nuc.example.lan", "nuc.example.lan"),
        (None, None),
        ("", None),
    ],
)
def test_host_of(header: str | None, host: str | None) -> None:
    assert dashboard.host_of(header) == host


def test_interval_seconds() -> None:
    assert dashboard.interval_seconds("1h 30m") == 5400
    assert dashboard.interval_seconds("2h") == 7200


@pytest.fixture
def server(settings: Settings, home: Path) -> Iterator[int]:
    from http.server import ThreadingHTTPServer

    hosts = dashboard.allowed_hosts("127.0.0.1", ["nuc.example.lan"])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(settings, home, hosts))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def get(port: int, path: str, host: str | None = None, method: str = "GET") -> Any:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": host} if host else {}
    conn.request(method, path, headers=headers)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp, body


def test_server_page(server: int) -> None:
    resp, body = get(server, "/?view=rated")
    assert resp.status == 200
    assert "script-src" not in resp.headers["Content-Security-Policy"]  # default-src 'none'
    assert resp.headers["Content-Security-Policy"].startswith("default-src 'none'")
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Server"] == "deal-hunter"
    assert b"Beelink SER5" in body and b"<script" not in body


def test_server_json(server: int) -> None:
    resp, body = get(server, "/api/summary.json", host="nuc.example.lan")
    data = json.loads(body)
    assert resp.status == 200 and resp.headers["Content-Type"] == "application/json"
    assert data["listings"]["matches"] == 1
    assert {r["id"] for r in data["results"]} == {"1", "2", "3", "4", "5"}
    comments = {r["id"]: r["comment"] for r in data["results"]}
    assert comments["4"] == "Not a PC—skip."


@pytest.mark.parametrize(
    ("path", "host", "method", "status"),
    [
        ("/", "evil.example", "GET", 421),  # DNS rebinding
        ("/", "evil.example:80", "GET", 421),
        ("/healthz", None, "GET", 200),
        ("/nope", None, "GET", 404),
        ("/", None, "POST", 501),
        ("/", None, "HEAD", 200),
    ],
)
def test_server_rejects(server: int, path: str, host: str | None, method: str, status: int) -> None:
    resp, _ = get(server, path, host, method)
    assert resp.status == status


def test_session_saved_before_metadata_existed(settings: Settings) -> None:
    session.save(FakeContext([LOGIN_COOKIE]), settings.session_file)
    session.meta_path(settings.session_file).unlink()
    assert "next logs in" in dashboard.session_info(settings, NOW)["problem"]


def test_junk_timestamps_render(settings: Settings, home: Path) -> None:
    events(home, {"ts": 1e300, "kind": "warning", "message": "x"})
    session.save(FakeContext([LOGIN_COOKIE]), settings.session_file)
    meta = session.meta_path(settings.session_file)
    meta.write_text(json.dumps({"saved_at": 1e300, "expires": -5}))
    assert "deal-hunter" in dashboard.render_page(dashboard.collect(settings, home, NOW), settings)
    assert dashboard.when(1e300) == "—" and dashboard.ago(1e300, NOW) == "never"


def test_unpriced_calls_make_spend_a_lower_bound(settings: Settings, home: Path) -> None:
    events(
        home,
        {"ts": NOW - 60, "kind": "ai_usage", "model": "claude-haiku-4-5", "input_tokens": 10**6},
        {"ts": NOW - 50, "kind": "ai_usage", "model": "mystery", "input_tokens": 10**6},
    )
    snap = dashboard.collect(settings, home, NOW)
    ai = dashboard.summary(snap, settings)["ai"]
    assert ai["cost_24h"] == pytest.approx(1.0) and ai["unpriced_calls"] == 1
    assert "≥ $1.00" in dashboard.render_page(snap, settings)


@pytest.mark.parametrize(
    "image",
    [
        "https://evil.example\\@x.fbcdn.net/a.jpg",
        "https://evil.example@x.fbcdn.net/a.jpg",
        "https://x.fbcdn.net/a b.jpg",
        "http://x.fbcdn.net/a.jpg",
        "https://fbcdn.net.evil.example/a.jpg",
    ],
)
def test_odd_photo_urls_are_dropped(image: str) -> None:
    r = dashboard.Result("1", "t", "$1", "", "", "", image, None)
    assert r.image_url(NOW) is None


def test_snapshot_cache(settings: Settings, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float] = []
    real = dashboard.collect

    def counting(*args: Any) -> Any:
        calls.append(1)
        return real(*args)

    monkeypatch.setattr(dashboard, "collect", counting)
    cache = dashboard.SnapshotCache(settings, home)
    first = cache.get()
    assert cache.get() is first and len(calls) == 1
    events(home, {"ts": NOW, "kind": "warning", "message": "new"})
    assert cache.get() is not first and len(calls) == 2


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"}, 403),
        ({"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}, 403),  # e.g. iframe
        (
            {
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
            },
            200,  # a link, or a login proxy's redirect back here
        ),
        ({"Sec-Fetch-Site": "same-origin"}, 200),
    ],
)
def test_server_refuses_cross_site_loads(server: int, headers: dict[str, str], status: int) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
    conn.request("GET", "/", headers=headers)
    assert conn.getresponse().status == status
    conn.close()


def test_metadata_lives_in_aimm_home_wherever_the_session_is(
    settings: Settings, tmp_path: Path
) -> None:
    # In a pod the session is on a volume `web` doesn't mount; its metadata isn't.
    elsewhere = tmp_path / "monitor-only" / "fb.json"
    session.AIMM_HOME.rmdir()  # a fresh pod: aimm hasn't created its home yet
    session.save(FakeContext([LOGIN_COOKIE]), elsewhere)
    assert session.meta_path(elsewhere).parent == session.AIMM_HOME
    assert session.meta_path(elsewhere).parent != elsewhere.parent


def test_healthz_answers_any_host(server: int) -> None:
    resp, body = get(server, "/healthz", host="10.0.0.7:8740")
    assert resp.status == 200 and body == b"ok\n"


def test_style_is_pinned_by_hash(server: int) -> None:
    resp, _ = get(server, "/")
    csp = resp.headers["Content-Security-Policy"]
    assert "unsafe-inline" not in csp and "style-src 'sha256-" in csp


def test_server_drops_connections_past_the_limit(settings: Settings, home: Path) -> None:
    import socket

    hosts = dashboard.allowed_hosts("127.0.0.1", [])
    srv = dashboard.BoundedServer(("127.0.0.1", 0), dashboard.make_handler(settings, home, hosts))
    srv.MAX_THREADS = 2
    srv.slots = threading.BoundedSemaphore(2)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    idle = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]  # slowloris
    try:
        third = socket.create_connection(("127.0.0.1", port))
        third.settimeout(5)
        third.sendall(b"GET /healthz HTTP/1.0\r\n\r\n")
        try:
            answer = third.recv(100)
        except ConnectionResetError:  # closed with unread bytes: an RST, not a FIN
            answer = b""
        assert answer == b""  # closed without an HTTP response
        third.close()
    finally:
        for s in idle:
            s.close()
        srv.shutdown()
        srv.server_close()


def test_importing_aimm_leaves_the_monitors_home_alone(tmp_path: Path) -> None:
    # aimm opens ~/.ai-marketplace-monitor/cache.db read-write when imported;
    # `web` must not, or it writes to the monitor's cache (and fails read-only).
    code = (
        "import os, sys; from deal_hunter import dashboard, session; "
        "dashboard.import_aimm_without_cache(); "
        "assert 'ai_marketplace_monitor.utils' in sys.modules; "
        "assert os.environ['HOME'] == sys.argv[1]; "
        "assert str(session.AIMM_HOME).startswith(sys.argv[1]); "
        "dashboard.import_aimm_without_cache()"  # already imported: a no-op
    )
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")}
    subprocess.run(  # noqa: S603
        [sys.executable, "-c", code, str(tmp_path)], env=env, check=True, timeout=60
    )
    assert not (tmp_path / ".ai-marketplace-monitor").exists()


def test_failed_aimm_import_restores_home(tmp_path: Path) -> None:
    code = (
        "import builtins, os, sys; from deal_hunter import dashboard; "
        "real = builtins.__import__\n"
        "def broken(name, *a, **k):\n"
        "    if name.startswith('deal_hunter.filters'): raise ImportError('boom')\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = broken\n"
        "try:\n"
        "    dashboard.import_aimm_without_cache()\n"
        "except ImportError: pass\n"
        "else: raise SystemExit('no error')\n"
        "assert os.environ['HOME'] == sys.argv[1]\n"
        "assert 'ai_marketplace_monitor.utils' not in sys.modules\n"
        "assert dashboard._SCRATCH_HOME is None\n"
    )
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")}
    subprocess.run(  # noqa: S603
        [sys.executable, "-c", code, str(tmp_path)], env=env, check=True, timeout=60
    )


class _Boom:
    def __reduce__(self) -> tuple[Any, ...]:
        return (os.system, ("touch /nonexistent-pwned",))


def _insert(conn: Any, key: Any, value: bytes, store_time: float = 0) -> None:
    import pickle

    conn.execute(
        "INSERT INTO Cache (key, raw, store_time, access_time, access_count, tag, "
        "size, mode, filename, value) VALUES (?, 0, ?, 0, 0, NULL, 0, 4, NULL, ?)",
        (pickle.dumps(key), store_time, value),
    )


def test_read_cache_refuses_code(tmp_path: Path) -> None:
    import pickle
    import sqlite3

    with Cache(str(tmp_path)) as cache:
        cache[("listing-details", "1")] = {"id": "1", "title": "NUC"}
    conn = sqlite3.connect(tmp_path / "cache.db")
    with conn:  # what a compromised monitor could write
        conn.execute("UPDATE Cache SET value = ?, mode = 4", (pickle.dumps(_Boom()),))
        conn.execute(
            "INSERT INTO Cache (key, raw, store_time, access_time, access_count, tag, "
            "size, mode, filename, value) VALUES (?, 0, 0, 0, 0, NULL, 0, 4, NULL, ?)",
            (pickle.dumps(_Boom()), pickle.dumps(1)),
        )
    conn.close()
    assert dashboard.read_cache(tmp_path) == []


def test_read_cache_caps_row_size_count_and_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pickle
    import sqlite3

    with Cache(str(tmp_path)):
        pass  # creates the schema
    small = pickle.dumps({"id": "ok"})
    big = pickle.dumps("x" * (dashboard.MAX_ROW_BYTES + 1))  # inline, not a file
    conn = sqlite3.connect(tmp_path / "cache.db")
    with conn:
        _insert(conn, ("listing-details", "big"), big, store_time=5)
        for i in range(5):
            _insert(conn, ("listing-details", str(i)), small, store_time=i)
    conn.close()
    keys = [k[1] for k, _, _ in dashboard.read_cache(tmp_path)]
    assert keys == ["0", "1", "2", "3", "4"]  # the oversized row is skipped, oldest first

    monkeypatch.setattr(dashboard, "MAX_ROWS", 2)
    assert [k[1] for k, _, _ in dashboard.read_cache(tmp_path)] == ["3", "4"]  # newest kept

    monkeypatch.setattr(dashboard, "MAX_ROWS", 100)
    monkeypatch.setattr(dashboard, "MAX_TOTAL_BYTES", 3 * (len(small) + 40))
    assert len(dashboard.read_cache(tmp_path)) < 5  # stops at the byte budget


def test_wildcard_bind_is_not_an_allowed_host() -> None:
    wildcard = "0.0.0.0"  # noqa: S104
    assert wildcard not in dashboard.allowed_hosts(wildcard, ["nuc.example"])
    assert "nuc.example" in dashboard.allowed_hosts(wildcard, ["nuc.example"])
    assert "10.1.2.3" in dashboard.allowed_hosts("10.1.2.3", [])


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/healthz", ""),
        ("GET", "/", ""),
        ("GET", "/nope", ""),
        ("HEAD", "/", ""),
        ("POST", "/", "x=1"),  # http.server's send_error path (501)
    ],
)
def test_server_speaks_http11_and_closes(server: int, method: str, path: str, body: str) -> None:
    import socket

    request = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
    if body:
        request += f"Content-Length: {len(body)}\r\n"
    request += f"\r\n{body}"
    with socket.create_connection(("127.0.0.1", server), timeout=5) as sock:
        sock.sendall(request.encode())
        data = b""
        while chunk := sock.recv(65536):  # ends only if the server closes
            data += chunk
    status, _, rest = data.partition(b"\r\n")
    assert status.startswith(b"HTTP/1.1 ")
    assert b"\r\nConnection: close\r\n" in rest


def test_watches_have_their_own_rating_and_filter(
    base_settings: dict[str, Any], tmp_path: Path
) -> None:
    base_settings["search"] = {}
    base_settings["watch"] = [
        {"name": "nuc", "preset": "mini-pc", "max_price": 400},
        {"name": "standing desk", "search_phrases": ["desk"], "max_price": 300, "rating": 5},
    ]
    base_settings["facebook"] = {"session_file": str(tmp_path / "fb.json")}
    settings = parse_settings(base_settings)
    home = tmp_path / "aimm"
    home.mkdir()
    with Cache(str(home)) as cache:
        put(cache, listing("1", "Beelink SER5"), 4)
        put(cache, listing("2", "Uplift standing desk"), 4, watch="standing desk")
        put(cache, listing("3", "Fully Jarvis desk"), 5, watch="standing desk")
        put(cache, listing("4", "Ikea desk lamp"), None)
    snap = dashboard.collect(settings, home, NOW)
    by_id = {r.id: r for r in snap.results}
    # Unrated: the first watch whose filters it passes (not nuc: no keyword).
    assert [by_id[i].watch for i in "1234"] == ["nuc"] + ["standing desk"] * 3
    ids = lambda view, watch=None: [  # noqa: E731
        r.id for r in dashboard.select(snap.results, view, settings, watch)
    ]
    assert ids("matches") == ["3", "1"]  # the desk rated 4 is under its watch's 5
    assert ids("rated", "standing desk") == ["3", "2"]
    page = dashboard.render_page(snap, settings, "rated", "standing desk")
    assert 'href="/?view=matches&amp;watch=standing%20desk"' in page
    assert "Fully Jarvis" in page and "Beelink" not in page
    assert "at each watch's rating" in page
    unknown = dashboard.render_page(snap, settings, "rated", "<script>")
    assert "Beelink" in unknown and "<script>" not in unknown


def test_rating_watch_decides_and_best_rating_wins(
    base_settings: dict[str, Any], tmp_path: Path
) -> None:
    base_settings["search"] = {}
    base_settings["watch"] = [
        {"name": "nuc", "preset": "mini-pc", "max_price": 400},
        {"name": "cheap pc", "search_phrases": ["pc"], "max_price": 100},
    ]
    base_settings["facebook"] = {"session_file": str(tmp_path / "fb.json")}
    settings = parse_settings(base_settings)
    home = tmp_path / "aimm"
    home.mkdir()
    with Cache(str(home)) as cache:
        both = listing("1", "Beelink mini pc", price="$90")
        put(cache, both, 3, "ok", watch="nuc")
        put(cache, both, 5, "steal", watch="cheap pc")
        # Rated when it was cheaper; the price now fails cheap pc but passes nuc.
        put(cache, listing("2", "Beelink EQ12", price="$150"), 4, watch="cheap pc")
    by_id = {r.id: r for r in dashboard.collect(settings, home, NOW).results}
    assert (by_id["1"].watch, by_id["1"].score, by_id["1"].comment) == ("cheap pc", 5, "steal")
    assert by_id["2"].watch == "cheap pc" and by_id["2"].dropped == ["price outside 0-100"]


def test_collect_reuses_filter_results(
    settings: Settings, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deal_hunter import filters

    memo: dict[tuple[str, ...], dict[str, list[str]]] = {}
    first = dashboard.collect(settings, home, NOW, memo)
    assert len(memo) == 5

    def boom(*args: object, **kwargs: object) -> list[str]:
        raise AssertionError("recomputed")

    monkeypatch.setattr(filters, "drop_reasons", boom)
    again = dashboard.collect(settings, home, NOW, memo)
    assert [r.dropped for r in again.results] == [r.dropped for r in first.results]


def test_problems_come_from_real_search_outcomes(settings: Settings, home: Path) -> None:
    # What the monitor records now: a per-phrase result, a warning only for an
    # empty one, and no "Failed to get search results" at all.
    events(
        home,
        {"ts": NOW - 30, "kind": "search_results", "item": "nuc", "phrase_index": 0, "count": 3},
        {"ts": NOW - 20, "kind": "search_results", "item": "nuc", "phrase_index": 1, "count": 0},
        {
            "ts": NOW - 19,
            "kind": "warning",
            "message": "No search results for watch 'nuc' phrase #1",
        },
    )
    snap = dashboard.collect(settings, home, NOW)
    problems = dashboard.summary(snap, settings)["problems"]
    assert [p["message"] for p in problems["recent"]] == [
        "No search results for watch 'nuc' phrase #1"
    ]
    assert problems["warnings_24h"] == 1 and problems["errors_24h"] == 0


# --- /metrics ---------------------------------------------------------------------


@pytest.fixture
def metrics_server(settings: Settings, home: Path) -> Iterator[tuple[int, dashboard.SnapshotCache]]:
    snapshots = dashboard.SnapshotCache(settings, home)
    srv = dashboard.start_metrics_server(settings, snapshots, "127.0.0.1", 0)
    yield srv.server_address[1], snapshots
    srv.shutdown()
    srv.server_close()


def test_metrics_scrape_by_pod_ip(metrics_server: tuple[int, Any], home: Path) -> None:
    port, _ = metrics_server
    now = time.time()  # the scrape's own clock: later events would count as forged
    events(
        home,
        *[
            {
                "ts": now - 100 + i,
                "kind": "search_results",
                "item": "nuc",
                "phrase_index": i,
                "count": c,
            }
            for i, c in enumerate([3, 0, 5, 2, 0, 1])
        ],  # fmt: skip
        {"ts": now - 90, "kind": "search_summary", "item": "nuc", "new_count": 0},
    )
    resp, body = get(port, "/metrics", host="10.1.2.3:9741")  # what Prometheus sends
    text = body.decode()
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "deal_hunter_build_info{" in text
    assert 'deal_hunter_searches_total{watch="nuc",outcome="failed"} 2' in text
    assert 'deal_hunter_searches_total{watch="nuc",outcome="ok"} 4' in text
    assert f"deal_hunter_last_success_timestamp_seconds {now - 90}" in text
    assert 'deal_hunter_listings{state="fetched"} 5' in text


@pytest.mark.parametrize("path", ["/", "/healthz", "/api/summary.json", "/?view=all", "/metrics/x"])
def test_metrics_port_serves_nothing_else(metrics_server: tuple[int, Any], path: str) -> None:
    port, _ = metrics_server
    resp, body = get(port, path, host="10.1.2.3:9741")
    assert resp.status == 404
    assert b"<" not in body and b"Beelink" not in body


def test_metrics_port_methods_and_head(metrics_server: tuple[int, Any]) -> None:
    port, _ = metrics_server
    assert get(port, "/metrics", method="POST")[0].status == 501
    resp, body = get(port, "/metrics", method="HEAD")
    assert resp.status == 200 and body == b""


def test_dashboard_port_still_enforces_host_and_has_no_metrics(server: int) -> None:
    assert get(server, "/metrics")[0].status == 404
    assert get(server, "/", host="10.1.2.3:8740")[0].status == 421


def test_dashboard_and_metrics_share_one_snapshot_cache(
    settings: Settings, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = dashboard.collect

    def counting(*args: Any) -> Any:
        calls.append(1)
        return real(*args)

    monkeypatch.setattr(dashboard, "collect", counting)
    snapshots = dashboard.SnapshotCache(settings, home)
    hosts = dashboard.allowed_hosts("127.0.0.1", [])
    from http.server import ThreadingHTTPServer

    page = ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(settings, home, hosts, snapshots)
    )
    threading.Thread(target=page.serve_forever, daemon=True).start()
    srv = dashboard.start_metrics_server(settings, snapshots, "127.0.0.1", 0)
    try:
        assert get(page.server_address[1], "/")[0].status == 200
        assert get(srv.server_address[1], "/metrics")[0].status == 200
        assert get(srv.server_address[1], "/metrics")[0].status == 200
    finally:
        page.shutdown()
        page.server_close()
        srv.shutdown()
        srv.server_close()
    assert len(calls) == 1  # one read of the cache for the page and both scrapes


def test_a_broken_snapshot_still_answers_without_a_trace(
    settings: Settings, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any) -> Any:
        raise RuntimeError("/home/secret/path exploded")

    monkeypatch.setattr(dashboard, "collect", boom)
    srv = dashboard.start_metrics_server(
        settings, dashboard.SnapshotCache(settings, home), "127.0.0.1", 0
    )
    try:
        resp, body = get(srv.server_address[1], "/metrics")
    finally:
        srv.shutdown()
        srv.server_close()
    assert resp.status == 200
    assert b"deal_hunter_cache_read_errors 1" in body
    assert b"secret" not in body and b"Traceback" not in body


def test_metrics_are_fast_on_a_big_cache(base_settings: dict[str, Any], tmp_path: Path) -> None:
    import time

    base_settings["search"] = {}
    base_settings["watch"] = [{"name": "desk", "search_phrases": ["desk"], "max_price": 900}]
    base_settings["facebook"] = {"session_file": str(tmp_path / "fb.json")}
    settings = parse_settings(base_settings)
    home = tmp_path / "big"
    home.mkdir()
    with Cache(str(home)) as cache:
        for n in range(20_000):
            put(cache, listing(str(n), f"Standing desk number {n}"), None)  # MAX_ROWS rows
    snapshots = dashboard.SnapshotCache(settings, home)
    srv = dashboard.start_metrics_server(settings, snapshots, "127.0.0.1", 0)
    try:
        started = time.perf_counter()
        resp, body = get(srv.server_address[1], "/metrics")
        first = time.perf_counter() - started
        assert snapshots.latest(60) is not None
        started = time.perf_counter()
        resp, body = get(srv.server_address[1], "/metrics")
        warm = time.perf_counter() - started
    finally:
        srv.shutdown()
        srv.server_close()
    assert resp.status == 200 and b'deal_hunter_listings{state="fetched"} 20000' in body
    assert first < 2.0 and warm < 2.0


def test_a_slow_read_does_not_hold_a_scrape(
    settings: Settings, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    snapshots = dashboard.SnapshotCache(settings, home)
    first = snapshots.latest(30)
    assert first is not None  # nothing slow yet
    release = threading.Event()
    real = dashboard.collect

    def slow(*args: Any) -> Any:
        release.wait(30)
        return real(*args)

    monkeypatch.setattr(dashboard, "collect", slow)
    events(home, {"ts": NOW, "kind": "warning", "message": "new"})  # the files changed
    started = time.perf_counter()
    assert snapshots.latest(0.05) is first  # the previous snapshot, not a wait
    assert time.perf_counter() - started < 1.0
    release.set()
    newer = snapshots.latest(30)
    assert newer is not first and newer is not None


def test_a_scrape_before_the_first_snapshot_exists_gets_the_stub(
    settings: Settings, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    real = dashboard.collect
    monkeypatch.setattr(dashboard, "collect", lambda *a: (release.wait(30), real(*a))[1])
    snapshots = dashboard.SnapshotCache(settings, home)
    srv = dashboard.start_metrics_server(settings, snapshots, "127.0.0.1", 0)
    monkeypatch.setattr(dashboard, "SCRAPE_WAIT_S", 0.05)
    try:
        _, body = get(srv.server_address[1], "/metrics")
        assert b"deal_hunter_cache_read_errors 1" in body
    finally:
        release.set()
        srv.shutdown()
        srv.server_close()


def test_a_failed_refresh_after_a_good_snapshot_is_reported(
    settings: Settings,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshots = dashboard.SnapshotCache(settings, home)
    srv = dashboard.start_metrics_server(settings, snapshots, "127.0.0.1", 0)
    port = srv.server_address[1]
    try:
        assert snapshots.latest(30) is not None
        _, body = get(port, "/metrics")
        assert b"deal_hunter_cache_read_errors 0" in body

        def boom(*args: Any) -> Any:
            raise RuntimeError("/home/secret/path exploded")

        monkeypatch.setattr(dashboard, "collect", boom)
        events(home, {"ts": NOW, "kind": "warning", "message": "changed"})
        for _ in range(2):
            _, body = get(port, "/metrics")
            assert b"deal_hunter_cache_read_errors 1" in body
            assert b'deal_hunter_listings{state="fetched"} 5' in body  # the last good snapshot
            assert b"secret" not in body
    finally:
        srv.shutdown()
        srv.server_close()
    assert snapshots.last_error == "RuntimeError"
    err = capsys.readouterr().err
    assert err.count("RuntimeError") == 1 and "secret" not in err  # said once, class only


@pytest.mark.parametrize("host", ["10.0.0.5:9741", "[::1]:9741", "10.0.0.5", "127.0.0.1:1"])
def test_metrics_serves_ip_literal_hosts(metrics_server: tuple[int, Any], host: str) -> None:
    assert get(metrics_server[0], "/metrics", host=host)[0].status == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:9741", "10.0.0.5.evil.example", ""])
def test_metrics_refuses_other_hosts(metrics_server: tuple[int, Any], host: str) -> None:
    for path in ("/metrics", "/"):
        assert get(metrics_server[0], path, host=host or "x y")[0].status == 403


def test_metrics_serves_the_dashboards_allowed_host_names(settings: Settings, home: Path) -> None:
    snapshots = dashboard.SnapshotCache(settings, home)
    hosts = dashboard.allowed_hosts("127.0.0.1", ["nuc.example.lan"])
    srv = dashboard.start_metrics_server(settings, snapshots, "127.0.0.1", 0, hosts)
    try:
        assert get(srv.server_address[1], "/metrics", host="nuc.example.lan:9741")[0].status == 200
        assert get(srv.server_address[1], "/metrics", host="evil.example")[0].status == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_future_event_cannot_make_the_monitor_look_running(
    settings: Settings, home: Path
) -> None:
    events(
        home,
        {"ts": NOW - 10 * 86_400, "kind": "search_summary", "item": "nuc", "new_count": 0},
        {"ts": NOW + 86_400, "kind": "search_summary", "item": "nuc", "new_count": 0},
    )
    snap = dashboard.collect(settings, home, NOW)
    assert dashboard.status(snap, settings).label == "Stalled or stopped"
