"""A timestamped record of what the monitor did, for `deal-hunter web`.

aimm's own log file has no timestamps and mixes in full API requests, so
``launch.main`` adds ``EventRecorder`` to the monitor's logging. It appends
one JSON line per useful event to ``deal-hunter-events.jsonl`` in aimm's home:
aimm's structured events (browser start, searches, AI ratings, skips),
token usage from each AI response, per-phrase search result counts, and
warnings and errors.

Messages are redacted before they're written: aimm can log a notifier's
token at WARNING ("Overriding ... with value ..."), so the values of secret-
looking environment variables are replaced with "[redacted]", and that
message is dropped. Token counts are read only from the response's own
usage block, never from the model's text. The file is owner-only and trimmed
to the newest half once it passes MAX_BYTES.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

EVENTS_NAME = "deal-hunter-events.jsonl"
# Its name before the project was renamed: `run` moves it, `web` reads it until then.
LEGACY_EVENTS_NAME = "nuc-hunter-events.jsonl"
MAX_BYTES = 2_000_000
MESSAGE_CHARS = 400

# Rich markup like [red][AI][/red]; keep the [AI] tag itself.
_MARKUP = re.compile(r"\[/?(?:red|green|blue|cyan|gray|grey|yellow|magenta|bold|dim|italic)\]")
# Token fields, Anthropic's names first, then OpenAI-style (OpenAI, Ollama).
_USAGE = {
    "input_tokens": ("input_tokens", "prompt_tokens"),
    "output_tokens": ("output_tokens", "completion_tokens"),
    "cache_read_tokens": ("cache_read_input_tokens",),
    "cache_write_tokens": ("cache_creation_input_tokens",),
}
_MODEL = re.compile(r"\bmodel='([\w.:/-]+)'")
_KINDS = {"search_summary", "browser_ready", "search_results"}
_FIELDS = {"item", "new_count", "engine", "sandbox", "phrase_index", "count"}
# Env vars whose values never go in the file.
_SECRET_ENV = re.compile(r"(KEY|TOKEN|PASSWORD|SECRET|TOPIC|CHAT_ID|USERNAME|CREDENTIALS?)$")
# A webhook URL ends in its token, which is a secret on its own (a log line may
# carry the token without the rest of the URL).
_WEBHOOK_ENV = re.compile(r"WEBHOOK(?:_URL)?$")
_OVERRIDE = re.compile(r"Overriding .* with value ")
# aimm 0.10.2 logs this error after every phrase search, whatever it found
# (tests/test_launch.py pins that). The truth is the search_results event.
# Matched at the start of a message logged by FacebookMarketplace.search itself:
# a seller can put the same words in a title that aimm logs elsewhere.
_FALSE_SEARCH_FAILURE = re.compile(r"\[Search\] Failed to get search results for ")


def secret_values() -> list[str]:
    """Secret-looking env values, and their URL-encoded forms, longest first."""
    values = {
        v
        for k, v in os.environ.items()
        if (_SECRET_ENV.search(k.upper()) or _WEBHOOK_ENV.search(k.upper())) and len(v) >= 4
    }
    for k, v in os.environ.items():
        if _WEBHOOK_ENV.search(k.upper()):
            token = v.strip().split("?")[0].rstrip("/").rpartition("/")[2]
            if len(token) >= 8:
                values.add(token)
    values |= {quote(v, safe="") for v in values}
    return sorted(values, key=len, reverse=True)


def plain(message: str) -> str:
    return _MARKUP.sub("", message)


class EventRecorder(logging.Handler):
    def __init__(
        self, path: Path, secrets: list[str] | None = None, hidden: list[str] | None = None
    ) -> None:
        super().__init__(level=logging.DEBUG)
        self.path = path
        self.secrets = secret_values() if secrets is None else secrets
        # Not secrets, but not for the events file either (the Marketplace
        # location). Matched without regard to case and only as a whole word
        # (so a short slug can't mangle other words or numbers), longest first.
        values = sorted((h for h in (hidden or []) if h.strip()), key=len, reverse=True)
        self.hidden = values
        self._hidden = [
            re.compile(rf"(?<![a-z0-9]){re.escape(v)}(?![a-z0-9])", re.IGNORECASE) for v in values
        ]

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[redacted]")
        for pattern in self._hidden:
            text = pattern.sub("[location]", text)
        return text.replace(str(Path.home()), "~")  # no username in the page

    def emit(self, record: logging.LogRecord) -> None:
        try:
            event = self.to_event(record)
            if event is not None:
                self.write(event)
        except Exception:  # noqa: BLE001 — never break the monitor over stats
            self.handleError(record)

    def to_event(self, record: logging.LogRecord) -> dict[str, Any] | None:
        ts = round(record.created, 3)
        aimm = getattr(record, "aimm", None)
        if isinstance(aimm, dict) and aimm.get("kind") in _KINDS:
            fields = {
                k: _short(self.redact(v) if isinstance(v, str) else v)
                for k, v in aimm.items()
                if k in _FIELDS
            }
            return {"ts": ts, "kind": aimm["kind"], **fields}
        message = record.getMessage()
        if message.startswith("[") and "[AI-Response]" in message[:40]:
            return ai_usage(message, ts)
        if record.levelno >= logging.WARNING:
            if _OVERRIDE.search(message) or (
                record.module == "facebook"
                and record.funcName == "search"
                and _FALSE_SEARCH_FAILURE.match(plain(message))
            ):
                return None
            kind = "error" if record.levelno >= logging.ERROR else "warning"
            return {"ts": ts, "kind": kind, "message": _short(self.redact(plain(message)))}
        return None

    def write(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(line)
            size = f.tell()
        if size > MAX_BYTES:
            self._trim()

    def _trim(self) -> None:
        from deal_hunter.files import write_private

        # Not a link or FIFO swapped in since write() opened it.
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, encoding="utf-8") as f:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return
            lines = f.read().splitlines(keepends=True)
        write_private(self.path, "".join(lines[len(lines) // 2 :]))


def ai_usage(message: str, ts: float) -> dict[str, Any] | None:
    """Token usage from aimm's pretty-printed API response.

    The response's text (which a listing can steer) comes before its model and
    usage fields, so only the last usage block, and the last model before it,
    count.
    """
    start = message.rfind("usage=")
    if start < 0:
        return None
    block = message[start:]
    usage: dict[str, Any] = {}
    for name, fields in _USAGE.items():
        for field_name in fields:
            if m := re.search(rf"\b{field_name}=(\d+)", block):
                usage[name] = int(m.group(1))
                break
    if "input_tokens" not in usage:
        return None
    models = _MODEL.findall(message[:start])
    return {"ts": ts, "kind": "ai_usage", "model": models[-1] if models else None, **usage}


def events_path(home: Path, *, migrate: bool = False) -> Path:
    """The events file in home, moving (migrate) or reading the old-named one if it's there."""
    path, legacy = home / EVENTS_NAME, home / LEGACY_EVENTS_NAME
    if os.path.lexists(path) or not os.path.lexists(legacy):
        return path
    if not migrate:
        return legacy
    try:
        os.rename(legacy, path)
    except OSError:
        pass  # start a new file; the old one stays where it is
    return path


def attach(logger: logging.Logger, path: Path, hidden: list[str] | None = None) -> None:
    """Record logger's events to path (once per logger), hiding the hidden values."""
    if not any(isinstance(h, EventRecorder) for h in logger.handlers):
        logger.addHandler(EventRecorder(path, hidden=hidden))


def _short(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MESSAGE_CHARS:
        return value[: MESSAGE_CHARS - 1] + "…"
    return value


# What `web` reads of the events file, whoever wrote it: the newest part only,
# and no single line big enough to matter.
READ_BYTES = 4 * MAX_BYTES
LINE_CHARS = 16_384


def read_events(path: Path, max_bytes: int = READ_BYTES) -> Iterator[dict[str, Any]]:
    """Events oldest first, from at most the last max_bytes of the file.

    Skips lines that aren't events (a torn last line) and overlong ones.
    """
    try:
        # Never a link: the monitor's side could point one anywhere web can read.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:  # missing, or a link
        return
    f = os.fdopen(fd, "rb")
    with f:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - max_bytes))
        data = f.read(max_bytes)
    lines = data.decode("utf-8", errors="replace").splitlines()  # a torn multibyte write
    if size > max_bytes and lines:
        lines = lines[1:]  # started mid-line
    for line in lines:
        if len(line) > LINE_CHARS:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and isinstance(event.get("ts"), (int, float)):
            yield event
