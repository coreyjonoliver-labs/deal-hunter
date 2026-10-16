"""OpenTelemetry traces for the monitor, off unless an OTLP endpoint is set.

``deal-hunter run`` can export one trace per search round (``deal_hunter.cycle``
and, under it, the phrase searches, page loads, listing fetches, AI ratings and
notifications) to any OTLP/HTTP collector. It answers *where* a round spent
its time or failed, which ``ops-metrics`` (counts) can't. Metrics stay on the
pull ``/metrics`` endpoint; OTel is used for traces only.

Traces leave the process, and the browser is untrusted, so what they carry is
decided here in code, not by convention:

- **An allowlist on export.** Every span passes through ``AllowlistSpanProcessor``
  before the batch exporter sees it. It keeps only the attribute keys in
  ``_VALIDATORS``, and only values that fit that key's validator (a watch name
  as settings.py validates it, small integers, a fixed set of outcomes, a
  provider or model name's characters). It drops events, links and exception
  messages, replaces any ERROR description with a fixed one, and renames an
  unknown span. So a bug that sets ``listing.title`` or ``url.full`` on a span,
  or puts a location in an exception message, still exports nothing. If a
  span can't be cleaned it is not exported.
- **No exception text.** A failed span records the exception's *type name*
  and ``"<span> failed"``. Messages from aimm, Playwright and HTTP clients can
  hold URLs, page text or the Marketplace location.
- **No propagation, no auto-instrumentation.** The global propagator is
  replaced with one that injects nothing, and nothing instruments ``requests``
  or ``httpx``, so no ``traceparent`` or ``baggage`` header goes to Facebook,
  the AI provider or a notifier.
- **Export problems are quiet and bounded.** The collector being down never
  stops the monitor: spans go through a background batch exporter, one line
  (never the endpoint, which can hold credentials, or the error text) is
  printed at most once a minute, and ``flush`` gives up after ``FLUSH_TIMEOUT_S``
  (the SDK's own ``force_flush`` ignores its timeout; see ``_bounded``).

Spans are opened and closed by ``span()`` with a stack of this module's own, not
by OTel's ambient context: aimm's ``search`` is a generator, and attaching and
detaching a context across its ``yield`` is not safe. Spans are opened only
around ordinary calls, in one thread.

The SDK is an optional extra (``uv sync --extra tracing``, installed in the
image), imported only once an endpoint is configured; without it ``setup``
prints one warning and the run is untraced. With nothing configured no OTel
object is created and every ``span()`` is a no-op.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import ipaddress
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from deal_hunter.settings import WATCH_NAME_RE, SettingsError

# Span names.
CYCLE = "deal_hunter.cycle"
SEARCH = "deal_hunter.search"
NAVIGATE = "deal_hunter.navigate"
LISTING = "deal_hunter.listing"
AI_RATE = "deal_hunter.ai_rate"
NOTIFY = "deal_hunter.notify"
SESSION_SAVE = "deal_hunter.session_save"
OTHER = "deal_hunter.other"  # what an unexpected name is exported as
SPAN_NAMES = frozenset({CYCLE, SEARCH, NAVIGATE, LISTING, AI_RATE, NOTIFY, SESSION_SAVE})

# The attribute keys that may leave the process (requirement 5).
WATCH = "deal_hunter.watch"
PHRASE_INDEX = "deal_hunter.phrase_index"
ATTEMPT = "deal_hunter.attempt"
OUTCOME = "deal_hunter.outcome"
RATING = "deal_hunter.rating"
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_MODEL = "gen_ai.request.model"
TOKENS_IN = "gen_ai.usage.input_tokens"
TOKENS_OUT = "gen_ai.usage.output_tokens"
STATUS_CODE = "http.response.status_code"
NOTIFY_METHOD = "notify.method"
EXCEPTION_TYPE = "exception.type"
COUNT_PREFIX = "deal_hunter.count."

OUTCOMES = frozenset({"ok", "failed", "empty", "cached", "sent", "skipped"})
FLUSH_TIMEOUT_S = 5.0
EXPORT_ERROR_INTERVAL_S = 60.0

_ENDPOINT_ENVS = ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
_HEADER_ENVS = ("OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS")
# A header pair as the SDK reads it: a token, "=", a value without quotes or controls.
_HEADER_PAIR = re.compile(r"\s*[A-Za-z0-9!#$%&'*+.^_`|~-]+\s*=[^\x00-\x1f\x7f\"]*")
_MAX_COUNT = 10**12

_COUNT_KEY = re.compile(r"deal_hunter\.count\.[a-z][a-z0-9_]{0,31}")
_PROVIDER = re.compile(r"[a-z][a-z0-9_.-]{0,31}")
_MODEL = re.compile(r"[A-Za-z0-9_.:/-]{1,80}")
_METHOD = re.compile(r"[a-z][a-z0-9_]{0,19}")
_TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def count(name: str) -> str:
    """The attribute key for a ``deal_hunter.count.<name>`` integer."""
    return COUNT_PREFIX + name


def _int(low: int, high: int) -> Callable[[Any], Any]:
    # type() is int: a bool is not a count.
    return lambda v: v if type(v) is int and low <= v <= high else None


def _match(pattern: re.Pattern[str]) -> Callable[[Any], Any]:
    # fullmatch: a "$" in a pattern would let a trailing newline through.
    return lambda v: v if isinstance(v, str) and pattern.fullmatch(v) else None


_VALIDATORS: dict[str, Callable[[Any], Any]] = {
    WATCH: _match(WATCH_NAME_RE),
    PHRASE_INDEX: _int(0, 1000),
    ATTEMPT: _int(0, 1000),
    OUTCOME: lambda v: v if isinstance(v, str) and v in OUTCOMES else None,
    RATING: _int(1, 5),
    GEN_AI_SYSTEM: _match(_PROVIDER),
    GEN_AI_MODEL: _match(_MODEL),
    TOKENS_IN: _int(0, _MAX_COUNT),
    TOKENS_OUT: _int(0, _MAX_COUNT),
    STATUS_CODE: _int(100, 599),
    NOTIFY_METHOD: _match(_METHOD),
    EXCEPTION_TYPE: _match(_TYPE_NAME),
}
_COUNT_VALUE = _int(0, _MAX_COUNT)


def clean_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """The allowlisted attributes of a span, with only values that fit their key."""
    clean: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if key in _VALIDATORS:
            checked = _VALIDATORS[key](value)
        elif _COUNT_KEY.fullmatch(key):
            checked = _COUNT_VALUE(value)
        else:
            continue
        if checked is not None:
            clean[key] = checked
    return clean


def failed_description(name: str) -> str:
    """The only description an ERROR status is exported with."""
    return f"{name if name in SPAN_NAMES else OTHER} failed"


class ExportErrors(logging.Handler):
    """Turns the OTel loggers' export errors into one fixed line a minute.

    The SDK logs the failing request (with the endpoint, which can hold
    credentials) and a traceback each time a batch fails. None of that is
    printed: only the exception's type name, and not more than once every
    ``interval_s``.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        interval_s: float = EXPORT_ERROR_INTERVAL_S,
    ) -> None:
        super().__init__(level=logging.WARNING)
        self._clock = clock
        self._interval_s = interval_s
        self._last: float | None = None

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        now = self._clock()
        if self._last is not None and now - self._last < self._interval_s:
            return
        self._last = now
        kind = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else None
        print(
            "deal-hunter: tracing: couldn't export spans to the collector"
            + (f" ({kind})" if kind else "")
            + ". Check OTEL_EXPORTER_OTLP_ENDPOINT. The monitor carries on; this is "
            "printed at most once a minute.",
            file=sys.stderr,
            flush=True,
        )


class SpanHandle:
    """What the code inside a ``span()`` block uses to describe the span."""

    __slots__ = ("_counts", "_has_outcome", "_span", "name")

    def __init__(self, span: Any, name: str) -> None:
        self._span = span
        self.name = name
        self._counts: dict[str, int] = {}
        self._has_outcome = False

    def set(self, key: str, value: Any) -> None:
        """Set an attribute. Only allowlisted keys and fitting values are exported."""
        if self._span is None or not isinstance(value, (str, int, float, bool)):
            return
        try:
            self._span.set_attribute(key, value)
        except Exception:  # noqa: BLE001, S110 — tracing must never break the monitor
            pass

    def outcome(self, value: str) -> None:
        self._has_outcome = True
        self.set(OUTCOME, value)

    def increment(self, key: str) -> None:
        self._counts[key] = self._counts.get(key, 0) + 1
        self.set(key, self._counts[key])

    def fail(self, exc: BaseException | None = None) -> None:
        """Mark the span failed: ERROR, the exception's type name, a fixed description."""
        if self._span is None:
            return
        if exc is not None:
            self.set(EXCEPTION_TYPE, type(exc).__name__)
        if not self._has_outcome:
            self.outcome("failed")
        try:
            self._span.set_status(_sdk.StatusCode.ERROR, failed_description(self.name))
        except Exception:  # noqa: BLE001, S110 — tracing must never break the monitor
            pass


_NOOP = SpanHandle(None, "")

# Set by setup, cleared by shutdown.
_sdk: Any = None
_provider: Any = None
_tracer: Any = None
_stack: list[SpanHandle] = []  # the open spans, innermost last
_previous_propagator: Any = None
_log_hook: tuple[logging.Handler, bool] | None = None  # (handler, the logger's old propagate)
_warned_missing = False


def enabled() -> bool:
    return _provider is not None


@contextlib.contextmanager
def span(
    name: str, attributes: Mapping[str, Any] | None = None, record: bool = True
) -> Iterator[SpanHandle]:
    """A span named ``name`` under the innermost open span (a no-op when tracing is off).

    An exception leaving the block marks the span failed (type name only) and
    propagates. ``record=False`` makes this one a no-op too, for a block that
    only sometimes turns out to be an event worth a span.
    """
    handle = _start(name, attributes) if record else _NOOP
    try:
        yield handle
    except BaseException as exc:
        handle.fail(exc)
        raise
    finally:
        _end(handle)


def annotate(name: str, key: str, value: Any) -> None:
    """Set an attribute on the innermost open span called ``name``, if there is one."""
    handle = _innermost(name)
    if handle is not None:
        handle.set(key, value)


def increment(name: str, key: str) -> None:
    """Add one to a counter attribute on the innermost open span called ``name``."""
    handle = _innermost(name)
    if handle is not None:
        handle.increment(key)


def _innermost(name: str) -> SpanHandle | None:
    for handle in reversed(_stack):
        if handle.name == name:
            return handle
    return None


def _start(name: str, attributes: Mapping[str, Any] | None) -> SpanHandle:
    if _tracer is None:
        return _NOOP
    try:
        parent = _stack[-1]._span if _stack else None
        context = _sdk.set_span_in_context(parent) if parent is not None else _sdk.Context()
        scalars = {
            k: v for k, v in (attributes or {}).items() if isinstance(v, (str, int, float, bool))
        }
        handle = SpanHandle(_tracer.start_span(name, context=context, attributes=scalars), name)
    except Exception:  # noqa: BLE001 — tracing must never break the monitor
        return _NOOP
    _stack.append(handle)
    return handle


def _end(handle: SpanHandle) -> None:
    if handle._span is None:
        return
    if handle not in _stack:  # tracing was stopped under it
        with contextlib.suppress(Exception):
            handle._span.end()
        return
    # Spans opened inside this one and never closed (they can't be, with
    # context managers) are ended with it rather than left to leak.
    while _stack:
        top = _stack.pop()
        try:
            top._span.end()
        except Exception:  # noqa: BLE001, S110 — tracing must never break the monitor
            pass
        if top is handle:
            return


def _raw_tracer() -> Any:
    """The SDK tracer itself, bypassing span() (tests of the allowlist use it)."""
    return _tracer


# --- Turning it on ----------------------------------------------------------------


def _endpoint_vars(env: Mapping[str, str]) -> list[str]:
    return [name for name in _ENDPOINT_ENVS if env.get(name, "").strip()]


def _wanted(env: Mapping[str, str]) -> bool:
    if not _endpoint_vars(env):
        return False
    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return False
    return env.get("OTEL_TRACES_EXPORTER", "").strip().lower() != "none"


def _check_headers(env: Mapping[str, str]) -> None:
    """SettingsError, not echoing the value, if a headers variable isn't ``k=v,k=v``.

    The SDK logs a malformed pair, value included, while it builds the exporter.
    Checked here first so a token can never reach a log through that path.
    """
    for name in _HEADER_ENVS:
        value = env.get(name, "")
        if not value.strip():
            continue
        if not all(_HEADER_PAIR.fullmatch(pair) for pair in value.split(",") if pair.strip()):
            raise SettingsError(f"{name} must be comma-separated key=value pairs.")


def _in_cluster_or_loopback(host: str) -> bool:
    if host == "localhost" or host.endswith((".svc", ".svc.cluster.local")):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_UNSAFE_URL = re.compile(r"[\\\s\x00-\x1f\x7f]")


def _connect_host(url: str) -> str | None:
    """The host urllib3 (what the exporter connects with) reads from url, lowercase."""
    try:
        from urllib3.util import parse_url

        host = parse_url(url).host
    except ImportError:  # pragma: no cover — requests, and so urllib3, come with the exporter
        return None
    except Exception:  # noqa: BLE001 — LocationParseError and friends: not a usable URL
        return ""
    return host.lower().strip("[]") if host else ""


def _parse(env: Mapping[str, str], name: str) -> tuple[str, int | None]:
    """(host, port) of the endpoint in env[name]; SettingsError, not echoing it, if it's no URL.

    A token (headers set, or credentials or a query in the URL) goes only over https, or
    over plain http to loopback or an in-cluster ``.svc`` name.
    """
    url = env[name].strip()
    try:
        parsed = urlparse(url)
        host, port = parsed.hostname, parsed.port
        credentials = bool(parsed.username or parsed.password or parsed.query)
    except ValueError:
        host, port, parsed, credentials = None, None, urlparse(""), False
    # urlparse and urllib3 (which the exporter connects with) read "http://a\\@b" as
    # different hosts. Refuse what could differ, and anything they still disagree on,
    # so the host checked (and printed) is the host connected to.
    if _UNSAFE_URL.search(url) or (host and _connect_host(url) not in (None, host.lower())):
        host = None
    if parsed.scheme not in ("http", "https") or not host:
        raise SettingsError(f"{name} must be an http:// or https:// URL with a host name.")
    has_token = credentials or any(env.get(h, "").strip() for h in _HEADER_ENVS)
    if parsed.scheme == "http" and has_token and not _in_cluster_or_loopback(host):
        raise SettingsError(
            f"{name} must be an https:// URL when headers or credentials are sent "
            "(plain http is allowed only to localhost or an in-cluster .svc address)."
        )
    return host, port


def destination(env: Mapping[str, str] | None = None) -> str | None:
    """``host[:port]`` the traces would go to, for the start-up line (never credentials or path)."""
    env = os.environ if env is None else env
    for name in reversed(_ENDPOINT_ENVS):  # the traces-specific one wins, as in the SDK
        if env.get(name, "").strip():
            host, port = _parse(env, name)
            return f"{host}:{port}" if port else host
    return None


def _load_sdk() -> SimpleNamespace:
    from opentelemetry.context import Context
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.propagators.composite import CompositePropagator
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace import SpanKind, Status, StatusCode, set_span_in_context

    class AllowlistSpanProcessor(SpanProcessor):
        """Passes each ended span to ``inner`` as a copy with only allowlisted content."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            self._inner.on_start(span, parent_context)

        def _on_ending(self, span: Any) -> None:
            pass

        def on_end(self, span: Any) -> None:
            try:
                name = span.name if span.name in SPAN_NAMES else OTHER
                code = span.status.status_code
                if code is StatusCode.ERROR:
                    status = Status(StatusCode.ERROR, failed_description(name))
                else:
                    status = Status(code)  # a description is only for errors, and ours
                clean = ReadableSpan(
                    name=name,
                    context=span.context,
                    parent=span.parent,
                    resource=span.resource,
                    attributes=clean_attributes(span.attributes),
                    events=(),
                    links=(),
                    kind=SpanKind.INTERNAL,
                    status=status,
                    start_time=span.start_time,
                    end_time=span.end_time,
                    instrumentation_scope=span.instrumentation_scope,
                )
            except Exception:  # noqa: BLE001 — a span that can't be cleaned is not exported
                return
            self._inner.on_end(clean)

        def shutdown(self) -> None:
            self._inner.shutdown()

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return bool(self._inner.force_flush(timeout_millis))

    return SimpleNamespace(
        Context=Context,
        OTLPSpanExporter=OTLPSpanExporter,
        CompositePropagator=CompositePropagator,
        Resource=Resource,
        TracerProvider=TracerProvider,
        BatchSpanProcessor=BatchSpanProcessor,
        AllowlistSpanProcessor=AllowlistSpanProcessor,
        StatusCode=StatusCode,
        set_span_in_context=set_span_in_context,
    )


def _version() -> str:
    try:
        return importlib.metadata.version("deal-hunter")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover — not installed
        return "unknown"


def _resource(sdk: Any) -> Any:
    """The SDK's environment-derived resource, with our service name and version as defaults."""
    base = sdk.Resource.create({})  # OTEL_SERVICE_NAME, OTEL_RESOURCE_ATTRIBUTES
    defaults = {}
    if str(base.attributes.get("service.name", "")).startswith("unknown_service"):
        defaults["service.name"] = "deal-hunter"
    if "service.version" not in base.attributes:
        defaults["service.version"] = _version()
    return base.merge(sdk.Resource(defaults)) if defaults else base


def _hook_logging() -> None:
    """Route the OTel loggers to ExportErrors alone (nothing reaches the monitor's logs)."""
    global _log_hook
    if _log_hook is not None:  # still there from an earlier run in this process
        return
    otel_logger = logging.getLogger("opentelemetry")
    handler = ExportErrors()
    _log_hook = (handler, otel_logger.propagate)
    otel_logger.addHandler(handler)
    otel_logger.propagate = False


def _unhook_logging() -> None:
    """Put the OTel loggers back. For setup's failure paths and tests only."""
    global _log_hook
    if _log_hook is not None:
        otel_logger = logging.getLogger("opentelemetry")
        otel_logger.removeHandler(_log_hook[0])
        otel_logger.propagate = _log_hook[1]
        _log_hook = None


def setup(env: Mapping[str, str] | None = None, *, exporter: Any = None) -> bool:
    """Start tracing if the environment asks for it; True if it is on.

    Off (and no OTel object created) unless OTEL_EXPORTER_OTLP_ENDPOINT or
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT is set; OTEL_SDK_DISABLED=true or
    OTEL_TRACES_EXPORTER=none keeps it off. Everything else about the export
    (headers, timeout, certificates, sampler, resource) is the SDK's usual
    OTEL_* variables. ``exporter`` replaces the OTLP exporter (tests).
    Raises SettingsError, without echoing the value, for an endpoint that isn't
    an http(s) URL, a malformed headers variable, or a token that would go over
    plain http to a host outside the cluster and loopback.
    """
    global _sdk, _provider, _tracer, _previous_propagator, _warned_missing
    if _provider is not None:
        return True
    env = os.environ if env is None else env
    if not _wanted(env):
        return False
    for name in _endpoint_vars(env):
        _parse(env, name)  # fail before anything starts
    _check_headers(env)
    # The logging hook goes on before any SDK object exists: the SDK logs what it
    # chokes on (a header, a resource attribute) while it is being built.
    _hook_logging()
    try:
        sdk = _load_sdk()
    except ImportError:
        _unhook_logging()
        if not _warned_missing:
            _warned_missing = True
            print(
                "deal-hunter: an OpenTelemetry endpoint is set, but the OpenTelemetry packages "
                "aren't installed, so this run is untraced. Install them with "
                "`uv sync --extra tracing` (or `pip install 'deal-hunter[tracing]'`).",
                file=sys.stderr,
                flush=True,
            )
        return False
    except Exception:  # noqa: BLE001 — the SDK failed to load; its message could hold a header
        _unhook_logging()
        raise SettingsError(
            "the OpenTelemetry SDK failed to start; check OTEL_* variables."
        ) from None
    try:
        # shutdown_on_exit=False: the SDK's atexit hook can wait 30 s on a dead collector.
        provider = sdk.TracerProvider(resource=_resource(sdk), shutdown_on_exit=False)
        provider.add_span_processor(
            sdk.AllowlistSpanProcessor(
                sdk.BatchSpanProcessor(exporter if exporter is not None else sdk.OTLPSpanExporter())
            )
        )
    except Exception:  # noqa: BLE001 — an invalid OTEL_* value; its text could hold a header
        _unhook_logging()
        raise SettingsError(
            "an OTEL_EXPORTER_OTLP_* or OTEL_TRACES_* variable is invalid."
        ) from None
    from opentelemetry import propagate

    _previous_propagator = propagate.get_global_textmap()
    propagate.set_global_textmap(sdk.CompositePropagator([]))  # injects nothing, extracts nothing
    _sdk, _provider = sdk, provider
    _tracer = provider.get_tracer("deal_hunter", _version())
    return True


# --- Flushing and stopping ---------------------------------------------------------


def _bounded(call: Callable[[], Any], timeout_s: float) -> bool:
    """Run call on a thread and wait at most timeout_s; True if it finished and said yes.

    The SDK's ``force_flush`` and ``shutdown`` take a timeout but the batch
    processor ignores it while an export is in flight, and an export to a
    collector that never answers takes the exporter's whole timeout (10 s by
    default). The abandoned thread is a daemon: it can't hold the process up.
    """
    result: list[bool] = []

    def run() -> None:
        try:
            result.append(bool(call()))
        except Exception:  # noqa: BLE001 — tracing must never break the monitor
            result.append(False)

    thread = threading.Thread(target=run, name="deal-hunter-trace-flush", daemon=True)
    thread.start()
    thread.join(max(timeout_s, 0))
    return bool(result) and result[0]


def flush(timeout_s: float = FLUSH_TIMEOUT_S) -> bool:
    """Export pending spans, giving up after timeout_s. True if tracing is off or all went."""
    provider = _provider
    if provider is None:
        return True
    return _bounded(lambda: provider.force_flush(int(timeout_s * 1000)), timeout_s)


def shutdown(timeout_s: float = FLUSH_TIMEOUT_S) -> None:
    """Flush, stop the exporter and restore the propagator; all within timeout_s."""
    global _sdk, _provider, _tracer
    provider = _provider
    if provider is None:
        return
    deadline = time.monotonic() + timeout_s
    flush(timeout_s)
    _bounded(provider.shutdown, deadline - time.monotonic())
    _sdk, _provider, _tracer = None, None, None
    _stack.clear()
    from opentelemetry import propagate

    propagate.set_global_textmap(_previous_propagator)
    # The logging hook stays: the batch worker and an abandoned flush thread can
    # still be exporting, and the SDK logs the failing URL (query token and all)
    # from them. Only the failure paths of setup() (nothing running yet) unhook.
