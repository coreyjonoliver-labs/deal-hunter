"""OpenTelemetry tracing: off unless asked for, an allowlist on all of it, no propagation."""

from __future__ import annotations

import http.server
import importlib.metadata
import itertools
import json
import logging
import socket
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry import baggage, propagate, trace
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from deal_hunter import launch, tracing
from deal_hunter.settings import SettingsError

ENDPOINT = "http://127.0.0.1:4318"
HOSTILE = "Springfield, IL: 555-0100 https://www.facebook.com/marketplace/item/123/"


@pytest.fixture
def memory(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    """Tracing on, exporting into memory."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    exporter = InMemorySpanExporter()
    assert tracing.setup(exporter=exporter) is True
    yield exporter
    tracing.shutdown(timeout_s=1)


def exported(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    assert tracing.flush()
    return list(exporter.get_finished_spans())


def dump(spans: list[ReadableSpan]) -> str:
    """Everything a span would export as text, except its random ids (which could contain
    any short digit string by chance)."""
    out = []
    for s in spans:
        data = json.loads(s.to_json())
        data.pop("context", None)
        data.pop("parent_id", None)
        out.append(json.dumps(data))
    return "\n".join(out)


# --- Off by default (requirement 1) ---------------------------------------------


def test_no_otel_variables_means_no_tracer_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden() -> Any:
        raise AssertionError("the SDK was loaded with tracing off")

    monkeypatch.setattr(tracing, "_load_sdk", forbidden)
    assert tracing.setup() is False
    assert tracing.enabled() is False
    assert tracing._provider is None
    # The OTel API's own global provider was never replaced either.
    assert not hasattr(trace.get_tracer_provider(), "force_flush")
    with tracing.span(tracing.CYCLE, {tracing.WATCH: "desk"}) as span:  # a no-op
        span.set(tracing.RATING, 3)
        span.outcome("ok")
        span.fail(ValueError("x"))
    tracing.annotate(tracing.CYCLE, tracing.ATTEMPT, 1)
    assert tracing.flush() is True  # nothing to flush


@pytest.mark.parametrize(
    ("env", "on"),
    [
        ({}, False),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": ENDPOINT}, True),
        ({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": ENDPOINT + "/v1/traces"}, True),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": ""}, False),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "  "}, False),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": ENDPOINT, "OTEL_SDK_DISABLED": "true"}, False),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": ENDPOINT, "OTEL_SDK_DISABLED": "TRUE"}, False),
        ({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": ENDPOINT, "OTEL_SDK_DISABLED": "True"}, False),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": ENDPOINT, "OTEL_SDK_DISABLED": "false"}, True),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": ENDPOINT, "OTEL_TRACES_EXPORTER": "none"}, False),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": ENDPOINT, "OTEL_TRACES_EXPORTER": "otlp"}, True),
        ({"OTEL_TRACES_EXPORTER": "otlp"}, False),
    ],
)
def test_when_tracing_is_on(monkeypatch: pytest.MonkeyPatch, env: dict[str, str], on: bool) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert tracing.setup(exporter=InMemorySpanExporter()) is on
    assert tracing.enabled() is on


def test_disabled_sdk_creates_no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setattr(tracing, "_load_sdk", lambda: pytest.fail("loaded"))
    assert tracing.setup() is False
    assert tracing._provider is None


def test_missing_packages_warn_once_and_run_untraced(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://user:s3cret@collector.example:4318")

    def missing() -> Any:
        raise ImportError("No module named 'opentelemetry'")

    monkeypatch.setattr(tracing, "_load_sdk", missing)
    assert tracing.setup() is False
    assert tracing.setup() is False  # a second call is quiet
    err = capsys.readouterr().err
    assert err.count("deal-hunter:") == 1
    assert "tracing" in err and "s3cret" not in err and "collector.example" not in err
    assert tracing.enabled() is False


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://collector.example",
        "collector.example:4318",
        "http://",
        "https://user:s3cret@",
        "x" * 5,
    ],
)
def test_a_malformed_endpoint_stops_the_run_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", endpoint)
    with pytest.raises(SettingsError) as raised:
        tracing.setup(exporter=InMemorySpanExporter())
    assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" in str(raised.value)
    assert "s3cret" not in str(raised.value) and "collector.example" not in str(raised.value)


def test_destination_names_the_host_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://u:s3cret@otel.example:4318/v1/x?k=v")
    assert tracing.destination() == "otel.example:4318"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://other.example/v1/traces")
    assert tracing.destination() == "other.example"  # the traces-specific one wins, as in the SDK
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    assert tracing.destination() is None


# --- Resource and sampler (requirement 2) ---------------------------------------


def test_resource_names_the_service_and_version(memory: InMemorySpanExporter) -> None:
    with tracing.span(tracing.CYCLE):
        pass
    [span] = exported(memory)
    assert span.resource.attributes["service.name"] == "deal-hunter"
    assert span.resource.attributes["service.version"] == importlib.metadata.version("deal-hunter")


def test_service_name_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_SERVICE_NAME", "hunter-staging")
    exporter = InMemorySpanExporter()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    assert tracing.setup(exporter=exporter)
    with tracing.span(tracing.CYCLE):
        pass
    [span] = exported(exporter)
    assert span.resource.attributes["service.name"] == "hunter-staging"
    assert span.resource.attributes["service.version"]


def test_the_sampler_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    exporter = InMemorySpanExporter()
    assert tracing.setup(exporter=exporter)
    with tracing.span(tracing.CYCLE):
        pass
    assert exported(exporter) == []


def test_default_sampling_keeps_every_cycle(memory: InMemorySpanExporter) -> None:
    for _ in range(5):
        with tracing.span(tracing.CYCLE):
            pass
    assert len(exported(memory)) == 5


# --- Span shapes (requirement 3, 4) ---------------------------------------------


def test_spans_nest_through_the_stack_not_ambient_context(memory: InMemorySpanExporter) -> None:
    with tracing.span(tracing.CYCLE, {tracing.WATCH: "desk"}):
        with tracing.span(tracing.SEARCH, {tracing.PHRASE_INDEX: 1}):
            with tracing.span(tracing.NAVIGATE):
                pass
        with tracing.span(tracing.NOTIFY):
            pass
    with tracing.span(tracing.NAVIGATE):  # a second, separate trace
        pass
    spans = {s.name + str(i): s for i, s in enumerate(exported(memory))}
    nav, search, notify, cycle, lone = spans.values()
    assert (nav.name, search.name, notify.name, cycle.name) == (
        tracing.NAVIGATE,
        tracing.SEARCH,
        tracing.NOTIFY,
        tracing.CYCLE,
    )
    assert cycle.parent is None and lone.parent is None
    assert search.parent is not None and search.parent.span_id == cycle.context.span_id
    assert nav.parent is not None and nav.parent.span_id == search.context.span_id
    assert notify.parent is not None and notify.parent.span_id == cycle.context.span_id
    assert len({s.context.trace_id for s in (nav, search, notify, cycle)}) == 1
    assert lone.context.trace_id != cycle.context.trace_id
    # Nothing was attached to the OTel context, so nothing leaks past the block.
    assert not trace.get_current_span().get_span_context().is_valid


def test_a_failure_records_the_type_and_a_fixed_description(memory: InMemorySpanExporter) -> None:
    with pytest.raises(RuntimeError):
        with tracing.span(tracing.NAVIGATE) as span:
            span.outcome("failed")
            raise RuntimeError(f"Failed to navigate to {HOSTILE}")
    [span_] = exported(memory)
    assert span_.status.status_code is StatusCode.ERROR
    assert span_.status.description == "deal_hunter.navigate failed"
    assert span_.attributes == {"exception.type": "RuntimeError", "deal_hunter.outcome": "failed"}
    assert span_.events == ()  # no exception event: its message and stack are not ours to send
    assert "Springfield" not in dump([span_]) and "facebook" not in dump([span_])


def test_keyboard_interrupt_ends_the_span_and_propagates(memory: InMemorySpanExporter) -> None:
    with pytest.raises(KeyboardInterrupt):
        with tracing.span(tracing.CYCLE):
            raise KeyboardInterrupt
    [span] = exported(memory)
    assert span.attributes is not None and span.attributes["exception.type"] == "KeyboardInterrupt"


def test_fail_without_an_exception_is_an_error_without_a_type(
    memory: InMemorySpanExporter,
) -> None:
    with tracing.span(tracing.NOTIFY, {tracing.NOTIFY_METHOD: "ntfy"}) as span:
        span.outcome("failed")
        span.fail()
    [done] = exported(memory)
    assert done.status.status_code is StatusCode.ERROR
    assert done.attributes == {"notify.method": "ntfy", "deal_hunter.outcome": "failed"}


def test_annotate_and_count_reach_the_innermost_open_span_of_that_name(
    memory: InMemorySpanExporter,
) -> None:
    with tracing.span(tracing.CYCLE):
        with tracing.span(tracing.AI_RATE):
            tracing.annotate(tracing.AI_RATE, tracing.TOKENS_IN, 120)
            tracing.increment(tracing.AI_RATE, tracing.ATTEMPT)
            tracing.increment(tracing.AI_RATE, tracing.ATTEMPT)
            tracing.annotate(tracing.CYCLE, tracing.count("new"), 2)
            tracing.annotate(tracing.NOTIFY, tracing.ATTEMPT, 9)  # none open: ignored
    ai, cycle = exported(memory)
    assert ai.attributes == {"gen_ai.usage.input_tokens": 120, "deal_hunter.attempt": 2}
    assert cycle.attributes == {"deal_hunter.count.new": 2}


def test_a_tracing_bug_never_breaks_the_monitor(
    memory: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("otel bug")

    monkeypatch.setattr(tracing._tracer, "start_span", broken)
    ran = []
    with tracing.span(tracing.CYCLE) as span:  # falls back to a no-op
        span.set(tracing.RATING, 3)
        ran.append(1)
    assert ran == [1]


# --- The allowlist (requirement 5) ----------------------------------------------

GOOD = {
    "deal_hunter.watch": "mini_pc",
    "deal_hunter.phrase_index": 2,
    "deal_hunter.attempt": 3,
    "deal_hunter.outcome": "ok",
    "deal_hunter.count.listings": 14,
    "deal_hunter.rating": 4,
    "gen_ai.system": "anthropic",
    "gen_ai.request.model": "claude-haiku-4-5",
    "gen_ai.usage.input_tokens": 812,
    "gen_ai.usage.output_tokens": 61,
    "http.response.status_code": 200,
    "notify.method": "ntfy",
    "exception.type": "TimeoutError",
}

FORBIDDEN = [
    "listing.title",
    "listing.description",
    "listing.id",
    "listing.url",
    "url.full",
    "url.path",
    "http.url",
    "http.target",
    "http.request.header.cookie",
    "http.request.header.authorization",
    "http.response.header.set-cookie",
    "server.address",
    "location",
    "seller.town",
    "deal_hunter.phrase",
    "deal_hunter.location",
    "deal_hunter.listing_id",
    "deal_hunter.url",
    "image.url",
    "gen_ai.prompt",
    "gen_ai.completion",
    "gen_ai.response.model",
    "ai.response",
    "ai.api_key",
    "notify.webhook_url",
    "notify.title",
    "exception.message",
    "exception.stacktrace",
    "enduser.id",
    "db.statement",
]


def one_span(exporter: InMemorySpanExporter, attributes: dict[str, Any], name: str = "") -> Any:
    tracer = tracing._raw_tracer()
    span = tracer.start_span(name or tracing.LISTING, attributes=attributes)
    span.end()
    [done] = exported(exporter)
    return done


def test_every_allowed_attribute_survives(memory: InMemorySpanExporter) -> None:
    assert dict(one_span(memory, dict(GOOD)).attributes) == GOOD


@pytest.mark.parametrize("key", FORBIDDEN)
def test_nothing_off_the_allowlist_is_exported(memory: InMemorySpanExporter, key: str) -> None:
    done = one_span(memory, {key: HOSTILE, "deal_hunter.watch": "desk"})
    assert dict(done.attributes) == {"deal_hunter.watch": "desk"}
    assert "Springfield" not in dump([done])


def test_attributes_set_after_the_start_and_by_every_route_are_filtered(
    memory: InMemorySpanExporter,
) -> None:
    tracer = tracing._raw_tracer()
    span = tracer.start_span(tracing.LISTING, attributes={"listing.title": "Uplift desk"})
    span.set_attribute("url.full", "https://www.facebook.com/marketplace/item/123/")
    span.set_attributes({"location": "Springfield", "deal_hunter.rating": 5})
    span.record_exception(RuntimeError(f"could not load {HOSTILE}"))
    span.add_event("page", {"text": HOSTILE})
    span.set_status(StatusCode.ERROR, f"Failed in {HOSTILE}")
    span.end()
    [done] = exported(memory)
    assert dict(done.attributes) == {"deal_hunter.rating": 5}
    assert done.events == ()
    assert done.links == ()
    assert done.status.status_code is StatusCode.ERROR
    assert done.status.description == "deal_hunter.listing failed"
    for leaked in ("Springfield", "facebook", "Uplift", "555", "could not load"):
        assert leaked not in dump([done])


def test_links_are_dropped(memory: InMemorySpanExporter) -> None:
    other = tracing._raw_tracer().start_span(tracing.LISTING)
    link = trace.Link(other.get_span_context(), {"url.full": HOSTILE})
    span = tracing._raw_tracer().start_span(tracing.LISTING, links=[link])
    span.end()
    other.end()
    assert all(s.links == () for s in exported(memory))


def test_an_unknown_span_name_is_not_exported_as_given(memory: InMemorySpanExporter) -> None:
    done = one_span(memory, {}, name=f"GET {HOSTILE}")
    assert done.name == "deal_hunter.other"
    assert "Springfield" not in dump([done])


def test_an_ok_status_is_kept_and_its_description_is_never_exported(
    memory: InMemorySpanExporter,
) -> None:
    span = tracing._raw_tracer().start_span(tracing.LISTING)
    span.set_status(StatusCode.OK)
    span.end()
    [done] = exported(memory)
    assert done.status.status_code is StatusCode.OK and done.status.description is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("deal_hunter.watch", "x\nSpringfield, IL"),
        ("deal_hunter.watch", "a" * 41),
        ("deal_hunter.watch", ""),
        ("deal_hunter.watch", 7),
        ("deal_hunter.watch", "https://www.facebook.com/marketplace/item/1/"),
        ("deal_hunter.phrase_index", -1),
        ("deal_hunter.phrase_index", "2"),
        ("deal_hunter.phrase_index", True),
        ("deal_hunter.phrase_index", 2.5),
        ("deal_hunter.attempt", 10**6),
        ("deal_hunter.outcome", HOSTILE),
        ("deal_hunter.outcome", "OK"),
        ("deal_hunter.count.listings", -3),
        ("deal_hunter.count.listings", "3"),
        ("deal_hunter.count.listings", 10**13),
        ("deal_hunter.count.Spring field", 3),
        ("deal_hunter.count.", 3),
        ("deal_hunter.count.listings.deeper", 3),
        ("deal_hunter.rating", 0),
        ("deal_hunter.rating", 6),
        ("deal_hunter.rating", "4"),
        ("deal_hunter.rating", 4.0),
        ("gen_ai.system", "My Provider"),
        ("gen_ai.system", HOSTILE),
        ("gen_ai.request.model", "model with spaces"),
        ("gen_ai.request.model", "m" * 81),
        ("gen_ai.usage.input_tokens", "812"),
        ("gen_ai.usage.input_tokens", -1),
        ("gen_ai.usage.output_tokens", True),
        ("http.response.status_code", 99),
        ("http.response.status_code", 600),
        ("http.response.status_code", "200"),
        ("notify.method", "https://discord.com/api/webhooks/1/token"),
        ("notify.method", "Ntfy"),
        ("exception.type", "Error: could not load Springfield"),
        ("exception.type", "9Lives"),
        ("exception.type", "E" * 65),
    ],
)
def test_an_allowed_key_with_a_hostile_value_is_dropped(
    memory: InMemorySpanExporter, key: str, value: Any
) -> None:
    assert dict(one_span(memory, {key: value}).attributes) == {}


def test_a_count_name_must_be_lowercase_words(memory: InMemorySpanExporter) -> None:
    done = one_span(memory, {"deal_hunter.count.new": 1, "deal_hunter.count.new_total": 2})
    assert dict(done.attributes) == {"deal_hunter.count.new": 1, "deal_hunter.count.new_total": 2}


# --- No propagation (requirement 6) ---------------------------------------------


class Echo(http.server.BaseHTTPRequestHandler):
    seen: list[dict[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802
        type(self).seen.append({k.lower(): v for k, v in self.headers.items()})
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def echo() -> Iterator[tuple[str, list[dict[str, str]]]]:
    Echo.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/", Echo.seen
    server.shutdown()
    server.server_close()


def test_the_global_propagator_injects_nothing(memory: InMemorySpanExporter) -> None:
    with tracing.span(tracing.CYCLE):
        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        # Even with trace context and baggage in play, nothing is written.
        with_baggage = baggage.set_baggage("listing", HOSTILE)
        propagate.inject(carrier, context=with_baggage)
        assert carrier == {}
    # Nor does an incoming header start a remote parent.
    incoming = propagate.extract({"traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01"})
    assert not trace.get_current_span(incoming).get_span_context().is_valid


def test_the_propagator_is_restored_when_tracing_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    before = propagate.get_global_textmap()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    assert tracing.setup(exporter=InMemorySpanExporter())
    assert propagate.get_global_textmap() is not before
    tracing.shutdown(timeout_s=1)
    assert propagate.get_global_textmap() is before


def test_an_outbound_request_during_a_traced_cycle_carries_no_trace_headers(
    memory: InMemorySpanExporter, echo: tuple[str, list[dict[str, str]]]
) -> None:
    import requests

    url, seen = echo
    with tracing.span(tracing.CYCLE):
        with tracing.span(tracing.AI_RATE):
            requests.get(url, timeout=5)
            urllib.request.urlopen(url, timeout=5).read()  # noqa: S310 — loopback http
    assert len(seen) == 2
    for headers in seen:
        assert not {"traceparent", "tracestate", "baggage"} & set(headers)


def test_no_http_auto_instrumentation_is_installed(memory: InMemorySpanExporter) -> None:
    assert not [m for m in sys.modules if m.startswith("opentelemetry.instrumentation")]
    installed = {d.metadata["Name"] for d in importlib.metadata.distributions()}
    assert not {n for n in installed if n.startswith("opentelemetry-instrumentation")}


# --- The exporter path against a fake OTLP collector (requirement 2) ------------


class Collector(http.server.BaseHTTPRequestHandler):
    received: list[tuple[str, dict[str, str], bytes]] = []

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).received.append(
            (self.path, {k.lower(): v for k, v in self.headers.items()}, body)
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def collector() -> Iterator[tuple[str, list[tuple[str, dict[str, str], bytes]]]]:
    Collector.received = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", Collector.received
    server.shutdown()
    server.server_close()


def test_spans_reach_an_otlp_http_collector_as_protobuf(
    monkeypatch: pytest.MonkeyPatch,
    collector: tuple[str, list[tuple[str, dict[str, str], bytes]]],
) -> None:
    url, received = collector
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", url)
    assert tracing.setup() is True
    with tracing.span(tracing.CYCLE, {tracing.WATCH: "desk"}):
        with tracing.span(tracing.SEARCH, {tracing.PHRASE_INDEX: 0}) as search:
            search.set("listing.title", HOSTILE)  # code error: dropped before export
    assert tracing.flush()
    [(path, headers, body)] = received
    assert path == "/v1/traces"
    assert headers["content-type"] == "application/x-protobuf"
    assert not {"traceparent", "tracestate", "baggage"} & set(headers)
    request = ExportTraceServiceRequest()
    request.ParseFromString(body)
    [resource_spans] = request.resource_spans
    names = {a.key: a.value.string_value for a in resource_spans.resource.attributes}
    assert names["service.name"] == "deal-hunter"
    spans = [s for scope in resource_spans.scope_spans for s in scope.spans]
    assert sorted(s.name for s in spans) == [tracing.CYCLE, tracing.SEARCH]
    search_span = next(s for s in spans if s.name == tracing.SEARCH)
    assert {a.key for a in search_span.attributes} == {"deal_hunter.phrase_index"}
    assert b"Springfield" not in body


def test_the_traces_specific_endpoint_is_used_as_given(
    monkeypatch: pytest.MonkeyPatch,
    collector: tuple[str, list[tuple[str, dict[str, str], bytes]]],
) -> None:
    url, received = collector
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", url + "/custom/path")
    assert tracing.setup() is True
    with tracing.span(tracing.CYCLE):
        pass
    assert tracing.flush()
    assert [r[0] for r in received] == ["/custom/path"]


# --- Collector down (requirement 7) ---------------------------------------------


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Blackhole:
    """Accepts connections and never answers."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = int(self.sock.getsockname()[1])
        self.held: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.held.append(conn)

    def close(self) -> None:
        self.sock.close()
        for conn in self.held:
            conn.close()


def test_flush_gives_up_within_the_bound_on_a_collector_that_never_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert tracing.FLUSH_TIMEOUT_S <= 5
    hole = Blackhole()
    try:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{hole.port}")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "4")
        assert tracing.setup() is True
        with tracing.span(tracing.CYCLE):
            pass
        started = time.monotonic()
        assert tracing.flush(timeout_s=0.5) is False
        assert time.monotonic() - started < 2
        started = time.monotonic()
        tracing.shutdown(timeout_s=0.5)
        assert time.monotonic() - started < 2
        assert tracing.enabled() is False
    finally:
        hole.close()


def test_an_unreachable_collector_does_not_stop_a_cycle_and_logs_without_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    port = closed_port()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://user:hunter2@127.0.0.1:{port}")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer tok-s3cret")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "1")
    assert tracing.setup() is True
    capsys.readouterr()
    started = time.monotonic()
    for _ in range(3):  # three cycles end; none waits for the collector
        with tracing.span(tracing.CYCLE):
            with tracing.span(tracing.NAVIGATE):
                pass
    assert time.monotonic() - started < 1
    tracing.flush(timeout_s=tracing.FLUSH_TIMEOUT_S)
    assert time.monotonic() - started < tracing.FLUSH_TIMEOUT_S + 2
    err = capsys.readouterr().err
    problems = [line for line in err.splitlines() if line.startswith("deal-hunter:")]
    assert len(problems) == 1
    for secret in ("hunter2", "s3cret", str(port), "127.0.0.1", "Traceback"):
        assert secret not in err


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def export_error(level: int = logging.ERROR, name: str = "opentelemetry.sdk.trace.export") -> Any:
    try:
        raise ConnectionError("connect to http://user:pw@host failed")
    except ConnectionError:
        return logging.LogRecord(
            name, level, __file__, 1, "Failed: %s", ("http://u:p@h",), sys.exc_info()
        )


def test_export_errors_are_printed_at_most_once_a_minute(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = Clock()
    handler = tracing.ExportErrors(clock=clock)
    for _ in range(5):
        handler.handle(export_error())
    clock.now += 59
    handler.handle(export_error())
    assert capsys.readouterr().err.count("deal-hunter:") == 1
    clock.now += 2  # 61 s since the first
    handler.handle(export_error())
    handler.handle(export_error(logging.WARNING))
    err = capsys.readouterr().err
    assert err.count("deal-hunter:") == 1
    assert "pw" not in err and "http" not in err and "ConnectionError" in err


def test_export_errors_ignore_quiet_levels(capsys: pytest.CaptureFixture[str]) -> None:
    handler = tracing.ExportErrors(clock=Clock())
    handler.handle(export_error(logging.INFO))
    handler.handle(export_error(logging.DEBUG))
    assert capsys.readouterr().err == ""


def test_otel_logging_stays_out_of_the_monitors_logs(
    memory: InMemorySpanExporter, capsys: pytest.CaptureFixture[str]
) -> None:
    root_records: list[logging.LogRecord] = []

    class Tap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            root_records.append(record)

    tap = Tap()
    logging.getLogger().addHandler(tap)
    try:
        logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter").error(
            "Failed to export to http://user:pw@collector/v1/traces"
        )
    finally:
        logging.getLogger().removeHandler(tap)
    assert root_records == []
    assert "pw" not in capsys.readouterr().err


def test_the_logging_hook_outlives_shutdown_so_late_export_logs_stay_quiet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The batch worker and an abandoned flush thread can still be exporting after
    # shutdown() returns; the SDK then logs the failing URL (with a query token).
    logger = logging.getLogger("opentelemetry")
    before = (list(logger.handlers), logger.propagate)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    assert tracing.setup(exporter=InMemorySpanExporter())
    assert logger.propagate is False
    tracing.shutdown(timeout_s=1)
    assert not tracing.enabled() and logger.propagate is False
    capsys.readouterr()
    late = logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    late.warning("Transient error ... url: /v1/traces?api_key=QUERYLEAK, retrying")
    late.error("Failed to export batch to https://u:QUERYLEAK@h/v1/traces?api_key=QUERYLEAK")
    shown = capsys.readouterr()
    assert "QUERYLEAK" not in shown.out + shown.err
    assert shown.err.count("deal-hunter: tracing") == 1  # the fixed, rate-limited line
    # Setting up again reuses the hook instead of stacking another.
    assert tracing.setup(exporter=InMemorySpanExporter())
    assert len([h for h in logger.handlers if isinstance(h, tracing.ExportErrors)]) == 1
    tracing.shutdown(timeout_s=1)
    tracing._unhook_logging()  # test-only: production keeps it for the life of the process
    assert (list(logger.handlers), logger.propagate) == before


def test_setup_twice_does_not_stack_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    assert tracing.setup(exporter=InMemorySpanExporter())
    first = tracing._provider
    assert tracing.setup(exporter=InMemorySpanExporter())
    assert tracing._provider is first


# --- A faked cycle through aimm's real code and deal-hunter's patch points ----------
#
# aimm's own MarketplaceMonitor.search_item, FacebookMarketplace.search, OpenAIBackend.evaluate
# and notify_all run for real; only the browser, the AI HTTP client and the notifier's
# transport are faked.

PHRASE = "secret standing desk phrase"
LOCATION = "springfieldtest"
TITLE = "Uplift V2 standing desk"
SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-api03-SECRETKEY0123456789",
    "NTFY_TOPIC": "my-private-topic-0123",
    "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1234567890/FAKE-token_0123456789abcdef",
}
ALLOWED_KEYS = set(GOOD) | {"deal_hunter.count.listings", "deal_hunter.count.new"}


# aimm's cache outlives a test (it is a module-level disk cache), and a listing it has
# already notified about is skipped: each faked cycle gets listing ids of its own.
LISTING_IDS = itertools.count(9100, 10)


def listing(n: int, base: int) -> Any:
    from ai_marketplace_monitor.listing import Listing

    return Listing(
        marketplace="facebook",
        name="desk",
        id=str(base + n),
        title=f"{TITLE} {n}",
        image="https://scontent.xx.fbcdn.net/v/t45/photo.jpg",
        price="$220",
        post_url=f"https://www.facebook.com/marketplace/item/{base + n}/",
        location="Springfield, IL",
        seller="someone",
        condition="Used - Good",
        description="60x30, light wear",
    )


class Page:
    """A Playwright page whose first load times out, with the text such an error carries."""

    def __init__(self, failures: int = 1) -> None:
        self.failures = failures

    def goto(self, url: str, *, timeout: float) -> Any:
        if self.failures:
            self.failures -= 1
            raise TimeoutError(f"Timeout 60000ms exceeded loading {url} for {HOSTILE}")
        return type("Response", (), {"status": 200})()

    def wait_for_load_state(self, state: str, *, timeout: float) -> None:
        pass


class Market:
    search = launch.search_with_session
    goto_url = launch.goto_url_with_timeout
    get_listing_details = launch.get_listing_details_traced

    def __init__(self, logger: logging.Logger, page: Page) -> None:
        self.page, self.translator, self.logger = page, None, logger
        self.keyboard_monitor, self.hunter_logged_in = None, False
        self.config = SimpleNamespace(
            search_city=[LOCATION],
            city_name=[None],
            **dict.fromkeys(
                [
                    "condition", "date_listed", "delivery_method", "availability", "sort_by",
                    "radius", "currency", "max_price", "min_price", "category",
                ]
            ),
        )  # fmt: skip

    def check_listing(self, *args: Any, **kwargs: Any) -> bool:
        return True


def item_config() -> Any:
    from deal_hunter import profile

    ns = SimpleNamespace(
        name="desk",
        marketplace="facebook",
        search_phrases=[PHRASE],
        searched_count=0,
        notify=["me"],
        rating=[3],
        ai=None,
        hash="item-hash",
        description="Electric",
        max_price=None,
        min_price=None,
        antikeywords=[],
        prompt=profile.GENERIC_PROMPT,
        extra_prompt=None,
        rating_prompt=profile.GENERIC_RATING_PROMPT,
        **dict.fromkeys(
            [
                "condition", "date_listed", "delivery_method", "availability", "sort_by",
                "search_city", "city_name", "radius", "currency", "category",
            ]
        ),
    )  # fmt: skip
    return ns


def completion(answer: str, model: str = "gpt-test") -> Any:
    from openai.types.chat import ChatCompletion

    return ChatCompletion.model_validate(
        {
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": answer},
                }
            ],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
        }
    )


class Cycle:
    """Everything a faked cycle needs, with the patch points installed as main() does."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, sent: bool = True) -> None:
        from ai_marketplace_monitor import ai, facebook, monitor
        from ai_marketplace_monitor.notification import NotificationConfig
        from ai_marketplace_monitor.ntfy import NtfyNotificationConfig
        from ai_marketplace_monitor.user import UserConfig

        monkeypatch.setattr(facebook.time, "sleep", lambda s: None)
        monkeypatch.setattr(monitor.time, "sleep", lambda s: None)
        monkeypatch.setattr(launch.time, "sleep", lambda s: None)
        monkeypatch.setattr(ai.time, "sleep", lambda s: None)
        monkeypatch.setattr(
            facebook.FacebookSearchResultPage, "get_listings", launch.get_listings_counted
        )
        self.base = next(LISTING_IDS)
        base = self.base
        monkeypatch.setattr(
            launch, "_upstream_get_listings", lambda page: [listing(1, base), listing(2, base)]
        )
        monkeypatch.setattr(
            launch,
            "_upstream_search",
            facebook.FacebookMarketplace.search,
        )
        monkeypatch.setattr(
            launch,
            "_upstream_get_listing_details",
            lambda self, url, item, price=None, title=None: (
                listing(int(url.rstrip("/").rpartition("/")[2]) - base, base),
                False,
            ),
        )
        monkeypatch.setattr(ai.OpenAIBackend, "evaluate", launch.openai_evaluate_traced)
        monkeypatch.setattr(ai.AIResponse, "to_cache", launch.to_cache_with_clean_summary)
        monkeypatch.setattr(monitor.MarketplaceMonitor, "search_item", launch.search_item_traced)
        monkeypatch.setattr(
            NotificationConfig, "send_message_with_retry", launch.send_message_recorded
        )
        monkeypatch.setattr(
            NotificationConfig, "notify_all", classmethod(launch.notify_all_with_discord)
        )
        monkeypatch.setattr(launch, "DISCORD", None)
        self.messages: list[str] = []

        def send_message(self_: Any, title: str, message: str, logger: Any = None) -> bool:
            if sent:
                self.messages.append(title)
            return sent

        monkeypatch.setattr(NtfyNotificationConfig, "send_message", send_message)
        self.logger = logging.getLogger("test-traced-cycle")
        self.logger.setLevel(logging.DEBUG)
        self.tap = launch.TraceTap()
        self.logger.addHandler(self.tap)
        self.page = Page()
        self.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kw: completion("Rating 4: Fair ~$350 vs $220 ask; buy")
                )
            )
        )
        backend = ai.OpenAIBackend(
            ai.OpenAIConfig(name="ai", api_key="k", provider="openai", model="gpt-test"),
            self.logger,
        )
        backend.client = self.client
        self.monitor = monitor.MarketplaceMonitor.__new__(monitor.MarketplaceMonitor)
        self.monitor.logger = self.logger
        self.monitor.ai_agents = [backend]
        self.monitor.config = SimpleNamespace(
            user={"me": UserConfig(name="me", ntfy_server="https://ntfy.example", ntfy_topic="t")}
        )
        self.market = Market(self.logger, self.page)

    def run(self) -> None:
        marketplace_config = SimpleNamespace(
            name="facebook", notify=None, rating=None, ai=None, hash="market-hash",
            prompt=None, extra_prompt=None, rating_prompt=None,
        )  # fmt: skip
        self.monitor.search_item(marketplace_config, self.market, item_config())

    def close(self) -> None:
        self.logger.removeHandler(self.tap)


@pytest.fixture
def cycle(monkeypatch: pytest.MonkeyPatch, memory: InMemorySpanExporter) -> Iterator[Cycle]:
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer tok-SECRETHEADER")
    run = Cycle(monkeypatch)
    yield run
    run.close()


def tree(spans: list[ReadableSpan]) -> dict[str, list[ReadableSpan]]:
    named: dict[str, list[ReadableSpan]] = {}
    for s in spans:
        named.setdefault(s.name, []).append(s)
    return named


def test_one_cycle_is_one_trace_with_search_navigate_ai_and_notify_children(
    cycle: Cycle, memory: InMemorySpanExporter
) -> None:
    cycle.run()
    spans = exported(memory)
    named = tree(spans)
    [root] = named[tracing.CYCLE]
    assert root.parent is None
    assert len({s.context.trace_id for s in spans}) == 1
    assert {s.name for s in spans} == {
        tracing.CYCLE,
        tracing.SEARCH,
        tracing.NAVIGATE,
        tracing.LISTING,
        tracing.AI_RATE,
        tracing.NOTIFY,
    }
    for child in spans:
        if child is not root:
            assert child.parent is not None and child.parent.span_id == root.context.span_id
    assert root.attributes == {
        "deal_hunter.watch": "desk",
        "deal_hunter.count.new": 2,
        "deal_hunter.outcome": "ok",
    }
    [search] = named[tracing.SEARCH]
    assert search.attributes == {
        "deal_hunter.watch": "desk",
        "deal_hunter.phrase_index": 0,
        "deal_hunter.count.listings": 2,
        "deal_hunter.outcome": "ok",
    }
    [nav] = named[tracing.NAVIGATE]  # the one search-page load: it timed out once, then loaded
    assert nav.attributes == {
        "deal_hunter.watch": "desk",
        "deal_hunter.phrase_index": 0,
        "deal_hunter.attempt": 1,
        "deal_hunter.outcome": "ok",
        "http.response.status_code": 200,
    }
    assert [s.attributes for s in named[tracing.LISTING]] == [{"deal_hunter.outcome": "ok"}] * 2
    ai_spans = named[tracing.AI_RATE]
    assert len(ai_spans) == 2
    assert ai_spans[0].attributes == {
        "gen_ai.system": "openai",
        "gen_ai.request.model": "gpt-test",
        "gen_ai.usage.input_tokens": 120,
        "gen_ai.usage.output_tokens": 30,
        "deal_hunter.rating": 4,
        "deal_hunter.outcome": "ok",
    }
    [notify] = named[tracing.NOTIFY]
    assert notify.attributes == {"notify.method": "ntfy", "deal_hunter.outcome": "sent"}
    assert all(s.status.status_code is not StatusCode.ERROR for s in spans)


def test_only_allowlisted_keys_leave_and_no_listing_text_or_secret_does(
    cycle: Cycle, memory: InMemorySpanExporter
) -> None:
    cycle.run()
    spans = exported(memory)
    assert {k for s in spans for k in (s.attributes or {})} <= ALLOWED_KEYS
    text = dump(spans)
    forbidden = [
        PHRASE, LOCATION, "Springfield", TITLE, "Uplift", "standing desk",
        str(cycle.base + 1), str(cycle.base + 2),
        "facebook.com", "fbcdn", "someone", "light wear", "Fair ~$350", "$220",
        "Timeout 60000ms", "555-0100", *SECRETS.values(), "SECRETHEADER", "authorization",
    ]  # fmt: skip
    for needle in forbidden:
        assert needle not in text, needle
    # And not from a failing run either (an exception message full of all of it).


def test_a_failed_notification_is_an_error_span_with_the_method(
    monkeypatch: pytest.MonkeyPatch, memory: InMemorySpanExporter
) -> None:
    run = Cycle(monkeypatch, sent=False)
    try:
        run.run()
    finally:
        run.close()
    [notify] = tree(exported(memory))[tracing.NOTIFY]
    assert notify.status.status_code is StatusCode.ERROR
    assert notify.status.description == "deal_hunter.notify failed"
    assert notify.attributes == {"notify.method": "ntfy", "deal_hunter.outcome": "failed"}


def test_a_failing_cycle_is_an_error_with_only_the_exception_type(
    cycle: Cycle, memory: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: Any, item: Any) -> Any:
        raise PermissionError(f"Facebook logged the monitor out at {HOSTILE}")

    monkeypatch.setattr(launch, "_upstream_search", broken)
    with pytest.raises(PermissionError):
        cycle.run()
    spans = exported(memory)
    [root] = tree(spans)[tracing.CYCLE]
    assert root.status.status_code is StatusCode.ERROR
    assert root.attributes == {
        "deal_hunter.watch": "desk",
        "deal_hunter.outcome": "failed",
        "exception.type": "PermissionError",
    }
    assert "Springfield" not in dump(spans) and "logged the monitor out" not in dump(spans)


def test_a_search_that_finds_nothing_says_so(
    cycle: Cycle, memory: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launch, "_upstream_get_listings", lambda page: [])
    cycle.run()
    [search] = tree(exported(memory))[tracing.SEARCH]
    assert search.attributes is not None
    assert search.attributes["deal_hunter.count.listings"] == 0
    assert search.attributes["deal_hunter.outcome"] == "empty"
    assert search.status.status_code is not StatusCode.ERROR


def test_a_cycle_with_tracing_off_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Cycle(monkeypatch)
    try:
        run.run()
    finally:
        run.close()
    assert tracing._provider is None
    assert run.messages == ["Found 2 new desks from facebook"]


def test_the_same_cycle_with_tracing_on_does_the_same_work(
    cycle: Cycle, memory: InMemorySpanExporter
) -> None:
    cycle.run()
    assert cycle.messages == ["Found 2 new desks from facebook"]
    assert len(exported(memory)) > 0


def test_a_page_that_never_loads_is_one_failed_navigate_span_with_its_retries(
    memory: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launch.time, "sleep", lambda s: None)
    market = Market(logging.getLogger("test-nav"), Page(failures=99))
    with pytest.raises(RuntimeError):
        market.goto_url("https://www.facebook.com/marketplace/item/1/")
    [nav] = exported(memory)
    assert nav.status.status_code is StatusCode.ERROR
    assert nav.attributes == {
        "deal_hunter.attempt": launch.NAV_RETRIES,
        "deal_hunter.outcome": "failed",
        "exception.type": "RuntimeError",
    }
    assert "facebook" not in dump([nav]) and "Springfield" not in dump([nav])


def test_a_page_without_a_browser_page_fails_the_same_way(
    memory: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launch.time, "sleep", lambda s: None)
    market = Market(logging.getLogger("test-nav"), Page())
    market.page = None  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        market.goto_url("https://www.facebook.com/")
    [nav] = exported(memory)
    assert nav.attributes is not None and nav.attributes["deal_hunter.attempt"] == 10


@pytest.mark.parametrize("error", [None, OSError(13, "Permission denied: /home/someone/fb.json")])
def test_saving_the_session_is_a_span(
    memory: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch, tmp_path: Any, error: Any
) -> None:
    from deal_hunter.session import Session

    def save(context: Any, path: Any) -> bool:
        if error is not None:
            raise error
        return True

    monkeypatch.setattr(launch.fb_session, "save", save)
    market = SimpleNamespace(page=SimpleNamespace(context=object()), logger=None)
    launch._save(market, Session(tmp_path / "fb.json", required=False))  # never raises
    [done] = exported(memory)
    assert done.name == tracing.SESSION_SAVE
    if error is None:
        assert done.attributes == {"deal_hunter.outcome": "ok"}
        assert done.status.status_code is not StatusCode.ERROR
    else:
        assert done.status.status_code is StatusCode.ERROR
        assert done.attributes == {
            "deal_hunter.outcome": "failed",
            "exception.type": "PermissionError",
        }
        assert "someone" not in dump([done]) and "fb.json" not in dump([done])


@pytest.fixture
def discord_posts(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    from ai_marketplace_monitor.notification import NotificationConfig

    from deal_hunter.discord import DiscordWebhook, Reply

    statuses: list[int] = []

    def transport(url: str, body: bytes, timeout: float) -> Reply:
        return Reply(statuses[0])

    monkeypatch.setattr(
        NotificationConfig, "notify_all", classmethod(launch.notify_all_with_discord)
    )
    monkeypatch.setattr(launch, "_upstream_notify_all", lambda cls, *a, **k: False)
    monkeypatch.setattr(
        launch,
        "DISCORD",
        DiscordWebhook(SECRETS["DISCORD_WEBHOOK_URL"], transport=transport, sleep=lambda s: None),
    )
    return statuses


def notify_discord(status_name: str) -> Any:
    from ai_marketplace_monitor import ai
    from ai_marketplace_monitor.notification import NotificationConfig, NotificationStatus
    from ai_marketplace_monitor.user import UserConfig

    rated = [ai.AIResponse(score=4, comment="Fair ~$350 vs $220 ask; buy", name="desk")]
    return NotificationConfig.notify_all(
        UserConfig(name="me"), [listing(1, 9000)], rated, [NotificationStatus[status_name]]
    )


@pytest.mark.parametrize(("status", "outcome"), [(204, "sent"), (404, "failed")])
def test_a_discord_post_is_a_notify_span(
    memory: InMemorySpanExporter, discord_posts: list[int], status: int, outcome: str
) -> None:
    discord_posts.append(status)
    notify_discord("NOT_NOTIFIED")
    [done] = exported(memory)
    assert done.name == tracing.NOTIFY
    assert done.attributes == {"notify.method": "discord", "deal_hunter.outcome": outcome}
    assert (done.status.status_code is StatusCode.ERROR) is (outcome == "failed")
    assert SECRETS["DISCORD_WEBHOOK_URL"] not in dump([done]) and "FAKE-token" not in dump([done])


def test_discord_with_everything_already_notified_is_not_a_span(
    memory: InMemorySpanExporter, discord_posts: list[int]
) -> None:
    discord_posts.append(204)
    notify_discord("NOTIFIED")
    assert exported(memory) == []


def test_a_discord_card_bug_is_an_error_span_of_its_type(
    memory: InMemorySpanExporter, discord_posts: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*a: Any, **k: Any) -> bool:
        raise KeyError(f"card for {HOSTILE}")

    monkeypatch.setattr(launch.discord, "notify", broken)
    notify_discord("NOT_NOTIFIED")
    [done] = exported(memory)
    assert done.status.status_code is StatusCode.ERROR
    assert done.attributes == {
        "notify.method": "discord",
        "deal_hunter.outcome": "failed",
        "exception.type": "KeyError",
    }
    assert "Springfield" not in dump([done])


def test_an_unknown_notifier_is_labelled_other(memory: InMemorySpanExporter) -> None:
    launch._recorded("evil\nmethod", None, lambda: True)
    [done] = exported(memory)
    assert done.attributes == {"notify.method": "other", "deal_hunter.outcome": "sent"}


def test_a_notifier_that_raises_is_an_error_span_and_still_raises(
    memory: InMemorySpanExporter,
) -> None:
    def boom() -> bool:
        raise ConnectionError(f"smtp down at {HOSTILE}")

    with pytest.raises(ConnectionError):
        launch._recorded("email", None, boom)
    [done] = exported(memory)
    assert done.status.status_code is StatusCode.ERROR
    assert done.attributes == {
        "notify.method": "email",
        "deal_hunter.outcome": "failed",
        "exception.type": "ConnectionError",
    }


# --- The AI backends and the log events that feed their spans --------------------


def evaluate_with(
    monkeypatch: pytest.MonkeyPatch, kind: str, create: Any, max_retries: int = 3
) -> tuple[Any, Any]:
    """aimm's real evaluate for kind, patched as main() does, with a fake HTTP client."""
    from ai_marketplace_monitor import ai

    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai.AIResponse, "to_cache", launch.to_cache_with_clean_summary)
    logger = logging.getLogger("test-ai")
    logger.setLevel(logging.DEBUG)
    tap = launch.TraceTap()
    logger.addHandler(tap)
    monkeypatch.setattr(ai.OpenAIBackend, "evaluate", launch.openai_evaluate_traced)
    monkeypatch.setattr(ai.AnthropicBackend, "evaluate", launch.anthropic_evaluate_traced)
    if kind == "anthropic":
        backend: Any = ai.AnthropicBackend(
            ai.AnthropicConfig(
                name="ai", api_key="k", provider="Anthropic", max_retries=max_retries
            ),
            logger,
        )
        backend.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    else:
        backend = ai.OpenAIBackend(
            ai.OpenAIConfig(name="ai", api_key="k", provider="deepseek", max_retries=max_retries),
            logger,
        )
        backend.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
    fake = backend.client

    def connect() -> None:  # aimm drops the client after an error and reconnects
        backend.client = fake

    backend.connect = connect
    base = next(LISTING_IDS)
    market = SimpleNamespace(hash="market-hash", prompt=None, extra_prompt=None, rating_prompt=None)
    return backend, (listing(1, base), item_config(), market, tap, logger)


def test_the_anthropic_backend_is_a_span_with_provider_model_and_tokens(
    memory: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    from anthropic.types import Message

    message = Message.model_validate(
        {
            "id": "m1",
            "type": "message",
            "role": "assistant",
            "model": "claude-test",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Rating 5: Fair ~$350 vs $220 ask; buy"}],
            "usage": {"input_tokens": 812, "output_tokens": 61},
        }
    )
    backend, (item, config, market, tap, logger) = evaluate_with(
        monkeypatch, "anthropic", lambda **kw: message
    )
    try:
        result = backend.evaluate(item, config, market)
    finally:
        logger.removeHandler(tap)
    assert result.score == 5
    [done] = exported(memory)
    assert done.name == tracing.AI_RATE
    assert done.attributes == {
        "gen_ai.system": "anthropic",
        "gen_ai.request.model": backend.default_model,
        "gen_ai.usage.input_tokens": 812,
        "gen_ai.usage.output_tokens": 61,
        "deal_hunter.rating": 5,
        "deal_hunter.outcome": "ok",
    }


def test_an_ai_retry_is_counted_and_the_error_text_never_leaves(
    memory: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def create(**kw: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError(f"Connection error to https://api.example/v1 for {HOSTILE}")
        return completion("Rating 3: Fair ~$350 vs $220 ask; ok")

    backend, (item, config, market, tap, logger) = evaluate_with(monkeypatch, "openai", create)
    try:
        backend.evaluate(item, config, market)
    finally:
        logger.removeHandler(tap)
    [done] = exported(memory)
    assert done.attributes is not None
    assert done.attributes["deal_hunter.attempt"] == 1
    assert done.attributes["gen_ai.system"] == "deepseek"
    assert done.attributes["gen_ai.usage.input_tokens"] == 120
    assert done.status.status_code is not StatusCode.ERROR
    assert "Springfield" not in dump([done]) and "api.example" not in dump([done])


def test_an_ai_backend_that_gives_up_is_an_error_span(
    memory: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    def create(**kw: Any) -> Any:
        raise TimeoutError(f"timed out for {HOSTILE}")

    backend, (item, config, market, tap, logger) = evaluate_with(
        monkeypatch, "openai", create, max_retries=2
    )
    try:
        with pytest.raises(NameError):  # aimm's own bug: `response` is never bound
            backend.evaluate(item, config, market)
    finally:
        logger.removeHandler(tap)
    [done] = exported(memory)
    assert done.status.status_code is StatusCode.ERROR
    assert done.attributes is not None
    assert done.attributes["exception.type"] == "UnboundLocalError"
    assert done.attributes["deal_hunter.attempt"] == 2
    assert done.attributes["deal_hunter.outcome"] == "failed"
    assert "Springfield" not in dump([done]) and "timed out" not in dump([done])


def record(message: str, **extra: Any) -> logging.LogRecord:
    # aimm logs these from OpenAIBackend/AnthropicBackend.evaluate.
    fields = {"msg": message, "levelno": logging.DEBUG, "funcName": "evaluate", **extra}
    return logging.makeLogRecord(fields)


def test_the_tap_reads_tokens_only_from_the_responses_own_usage_block(
    memory: InMemorySpanExporter,
) -> None:
    dumped = (
        "[blue][AI-Response][/blue] Message(id='m', content=[TextBlock(text='Rating 4: "
        "usage=Usage(input_tokens=999999, output_tokens=999999)', type='text')], "
        "model='claude-x', usage=Usage(input_tokens=812, output_tokens=61))"
    )
    with tracing.span(tracing.AI_RATE):
        launch.TraceTap().emit(record(dumped))
    [done] = exported(memory)
    assert done.attributes == {"gen_ai.usage.input_tokens": 812, "gen_ai.usage.output_tokens": 61}


def test_a_listing_cannot_forge_an_ai_event_far_into_a_line(
    memory: InMemorySpanExporter,
) -> None:
    tap = launch.TraceTap()
    with tracing.span(tracing.AI_RATE):
        tap.emit(
            record(
                "[Retrieve] New item " + "x" * 60 + " [AI-Error] [AI-Response] usage=input_tokens=5"
            )
        )
        tap.emit(record("[AI-Response] no usage block here"))
    [done] = exported(memory)
    assert done.attributes == {}


def test_the_tap_counts_ai_errors_and_the_new_listing_count(memory: InMemorySpanExporter) -> None:
    tap = launch.TraceTap()
    with tracing.span(tracing.CYCLE):
        with tracing.span(tracing.AI_RATE):
            tap.emit(record("[red][AI-Error][/red] ai failed to evaluate x: y"))
            tap.emit(record("[red][AI-Error][/red] ai failed to evaluate x: y"))
        tap.emit(record("found", aimm={"kind": "search_summary", "new_count": 3, "item": "desk"}))
        tap.emit(record("other", aimm={"kind": "ai_eval", "title": TITLE, "score": 4}))
    ai_span, cycle = exported(memory)
    assert ai_span.attributes == {"deal_hunter.attempt": 2}
    assert cycle.attributes == {"deal_hunter.count.new": 3}


def test_the_tap_ignores_odd_new_counts_and_never_raises(memory: InMemorySpanExporter) -> None:
    tap = launch.TraceTap()
    with tracing.span(tracing.CYCLE):
        tap.emit(record("x", aimm={"kind": "search_summary", "new_count": HOSTILE}))
        tap.emit(record("x", aimm={"kind": "search_summary", "new_count": -4}))
        tap.emit(record("x", aimm={"kind": "search_summary"}))
        tap.emit(record(None))  # type: ignore[arg-type]
    [done] = exported(memory)
    assert done.attributes == {}


def test_the_tap_does_nothing_with_tracing_off() -> None:
    launch.TraceTap().emit(record("[AI-Error] x"))
    launch.TraceTap().emit(record("x", aimm={"kind": "search_summary", "new_count": 1}))


# --- Headers, https and a logging hook that is there before the SDK is (security) ---

MALFORMED_HEADERS = [
    "Authorization: Bearer SUPERSECRET123",
    'x-api-key=abc"def-SUPERSECRET123',
    "a=b,SUPERSECRET123",
    "=SUPERSECRET123",
    "k=SUPERSECRET123\x01",
    "bad key=SUPERSECRET123",
]


@pytest.mark.parametrize(
    "name", ["OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS"]
)
@pytest.mark.parametrize("value", MALFORMED_HEADERS)
def test_malformed_headers_stop_the_run_and_print_no_part_of_them(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example:4318")
    monkeypatch.setenv(name, value)
    with pytest.raises(SettingsError) as raised:
        tracing.setup()
    assert name in str(raised.value)
    shown = capsys.readouterr()
    for text in (str(raised.value), shown.out, shown.err):
        assert "SUPERSECRET123" not in text and "abc" not in text and "Bearer" not in text
    assert not tracing.enabled()


def test_well_formed_headers_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example:4318")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer tok-1, x-api-key=k%20v,empty="
    )
    assert tracing.setup(exporter=InMemorySpanExporter()) is True


@pytest.mark.parametrize("failure", [ImportError("nope"), RuntimeError("SUPERSECRET123 invalid")])
def test_the_logging_hook_is_on_before_the_sdk_is_built_and_off_after_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: Exception
) -> None:
    otel = logging.getLogger("opentelemetry")
    before = (list(otel.handlers), otel.propagate)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example:4318")

    def noisy() -> Any:
        # What the SDK does while parsing a bad header: log it, value and all.
        logging.getLogger("opentelemetry.util.re").warning("Invalid header SUPERSECRET123")
        raise failure

    monkeypatch.setattr(tracing, "_load_sdk", noisy)
    try:
        tracing.setup()
    except SettingsError:
        pass
    shown = capsys.readouterr()
    assert "SUPERSECRET123" not in shown.out + shown.err
    assert (list(otel.handlers), otel.propagate) == before


def test_building_the_provider_runs_with_the_hook_and_a_failure_restores_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    otel = logging.getLogger("opentelemetry")
    before = (list(otel.handlers), otel.propagate)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example:4318")
    real = tracing._resource

    def noisy(sdk: Any) -> Any:
        logging.getLogger("opentelemetry.sdk.resources").warning("bad SUPERSECRET123")
        raise ValueError("SUPERSECRET123")

    monkeypatch.setattr(tracing, "_resource", noisy)
    with pytest.raises(SettingsError) as raised:
        tracing.setup(exporter=InMemorySpanExporter())
    monkeypatch.setattr(tracing, "_resource", real)
    shown = capsys.readouterr()
    assert "SUPERSECRET123" not in shown.out + shown.err + str(raised.value)
    assert (list(otel.handlers), otel.propagate) == before


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:4318",
        "http://localhost:4318",
        "http://[::1]:4318",
        "http://otel.observability.svc:4318",
        "http://otel.observability.svc.cluster.local:4318",
        "https://collector.example:4318",
    ],
)
@pytest.mark.parametrize("carrier", ["headers", "userinfo"])
def test_a_token_may_travel_over_https_loopback_or_in_cluster_http(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, carrier: str
) -> None:
    if carrier == "headers":
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer tok-1")
    else:
        endpoint = endpoint.replace("//", "//user:pw@", 1)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    assert tracing.setup(exporter=InMemorySpanExporter()) is True


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://collector.example:4318",
        "http://203.0.113.9:4318",
        "http://evil.svc.example.com:4318",
        "http://svc.cluster.local:4318",
        "http://notlocalhost:4318",
        "http://127.0.0.1.example.com:4318",
    ],
)
@pytest.mark.parametrize("carrier", ["headers", "traces_headers", "userinfo"])
def test_a_token_over_plain_http_to_another_host_is_refused_without_echo(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, carrier: str
) -> None:
    if carrier == "headers":
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer SUPERSECRET123")
    elif carrier == "traces_headers":
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "authorization=SUPERSECRET123")
    else:
        endpoint = endpoint.replace("//", "//user:SUPERSECRET123@", 1)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    with pytest.raises(SettingsError) as raised:
        tracing.setup(exporter=InMemorySpanExporter())
    message = str(raised.value)
    assert "https" in message and "OTEL_EXPORTER_OTLP_ENDPOINT" in message
    assert "SUPERSECRET123" not in message and "example" not in message and "203" not in message


def test_plain_http_without_a_token_is_still_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.example:4318")
    assert tracing.setup(exporter=InMemorySpanExporter()) is True


# --- Page loads say which phrase they were for -------------------------------------


@pytest.mark.parametrize(
    ("url", "attributes"),
    [
        (
            "https://www.facebook.com/marketplace/city/search?query=x",
            {"deal_hunter.watch": "desk", "deal_hunter.phrase_index": 1},
        ),
        (
            "https://www.facebook.com/marketplace/item/9/",
            {"deal_hunter.watch": "desk", "deal_hunter.phrase_index": 0},  # the phrase just parsed
        ),
        ("https://www.facebook.com/marketplace/", {"deal_hunter.watch": "desk"}),
    ],
)
def test_a_page_load_during_a_search_carries_the_watch_and_phrase(
    memory: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    attributes: dict[str, Any],
) -> None:
    monkeypatch.setattr(launch._Search, "watch", "desk")
    monkeypatch.setattr(launch._Search, "phrases", 2)
    monkeypatch.setattr(launch._Search, "phrases_done", 1)
    Market(logging.getLogger("t"), Page(failures=0)).goto_url(url)
    [nav] = exported(memory)
    assert dict(nav.attributes or {}) == {
        **attributes,
        "deal_hunter.attempt": 0,
        "deal_hunter.outcome": "ok",
        "http.response.status_code": 200,
    }
    assert "facebook" not in dump([nav])


def test_a_page_load_outside_a_search_has_no_phrase(memory: InMemorySpanExporter) -> None:
    Market(logging.getLogger("t"), Page(failures=0)).goto_url("https://www.facebook.com/")
    [nav] = exported(memory)
    assert "deal_hunter.phrase_index" not in (nav.attributes or {})
    assert "deal_hunter.watch" not in (nav.attributes or {})


def test_a_failed_search_page_load_shows_which_phrase(
    memory: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launch.time, "sleep", lambda s: None)
    monkeypatch.setattr(launch._Search, "watch", "desk")
    monkeypatch.setattr(launch._Search, "phrases", 3)
    monkeypatch.setattr(launch._Search, "phrases_done", 5)  # two cities: 5 % 3
    with pytest.raises(RuntimeError):
        Market(logging.getLogger("t"), Page(failures=99)).goto_url(
            "https://www.facebook.com/marketplace/c/search?query=x"
        )
    [nav] = exported(memory)
    assert nav.attributes is not None
    assert nav.attributes["deal_hunter.phrase_index"] == 2
    assert nav.status.status_code is StatusCode.ERROR


# --- The tap trusts only aimm's own AI log lines -----------------------------------


@pytest.mark.parametrize(
    ("message", "func"),
    [
        ("[Skip] Already sent notification for item [AI-Error] x", "evaluate"),
        ("[Retrieve] New item [AI-Error] x", "search"),
        ("[red][AI-Error][/red] fake", "search_item"),  # the right shape from the wrong place
        ("x [red][AI-Error][/red]", "evaluate"),
        ("[AI-Error] no markup", "evaluate"),
    ],
)
def test_a_listing_title_or_other_log_line_cannot_bump_the_attempt_count(
    memory: InMemorySpanExporter, message: str, func: str
) -> None:
    with tracing.span(tracing.AI_RATE):
        launch.TraceTap().emit(record(message, funcName=func))
    [done] = exported(memory)
    assert done.attributes == {}


def test_a_forged_response_dump_from_another_function_gives_no_tokens(
    memory: InMemorySpanExporter,
) -> None:
    dumped = "[blue][AI-Response][/blue] Message(usage=Usage(input_tokens=5, output_tokens=6))"
    with tracing.span(tracing.AI_RATE):
        launch.TraceTap().emit(record(dumped, funcName="get_listing_details"))
    [done] = exported(memory)
    assert done.attributes == {}


# --- One set of pins ------------------------------------------------------------------


def test_the_tracing_extra_and_the_dev_group_pin_the_same_otel_packages() -> None:
    from pathlib import Path

    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover
        import tomli as tomllib

    data = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    extra = data["project"]["optional-dependencies"]["tracing"]
    dev = [d for d in data["dependency-groups"]["dev"] if d.startswith("opentelemetry-")]
    assert sorted(extra) == sorted(dev) and len(extra) == 3
    assert all("==" in pin for pin in extra)


# --- Parser differential, query tokens ------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://evil.example\\@x.svc:4318",
        "http://evil.example\\@localhost:4318",
        "http://x.svc :4318",
        "http://x.svc\t.evil.example:4318",
        "http://x.svc\n@evil.example",
        "http://x.svc\x01.evil.example",
        "http://x.svc:4318/\\@evil.example",
    ],
)
def test_endpoints_the_two_url_parsers_could_read_differently_are_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], endpoint: str
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    with pytest.raises(SettingsError) as raised:
        tracing.setup(exporter=InMemorySpanExporter())
    assert "evil" not in str(raised.value) and "OTEL_EXPORTER_OTLP_ENDPOINT" in str(raised.value)
    with pytest.raises(SettingsError):
        tracing.destination()
    assert "evil" not in capsys.readouterr().err


def test_the_host_urllib3_connects_to_is_the_host_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib3.util import parse_url

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel.observability.svc:4318/v1")
    assert tracing.destination() == "otel.observability.svc:4318"
    assert parse_url("http://otel.observability.svc:4318/v1").host == "otel.observability.svc"
    # If the two parsers ever disagree on a URL that gets past the character check,
    # it is refused rather than guessed at.
    monkeypatch.setattr(tracing, "_connect_host", lambda url: "somewhere.else.example")
    with pytest.raises(SettingsError):
        tracing.destination()


@pytest.mark.parametrize(
    "name", ["OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]
)
@pytest.mark.parametrize("host", ["collector.example", "203.0.113.9"])
def test_a_query_string_counts_as_a_token_over_plain_http(
    monkeypatch: pytest.MonkeyPatch, name: str, host: str
) -> None:
    monkeypatch.setenv(name, f"http://{host}:4318/v1/traces?api_key=QUERYLEAK")
    with pytest.raises(SettingsError) as raised:
        tracing.setup(exporter=InMemorySpanExporter())
    assert "https" in str(raised.value) and "QUERYLEAK" not in str(raised.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://collector.example:4318/v1/traces?api_key=k",
        "http://127.0.0.1:4318/v1/traces?x=1",
        "http://otel.observability.svc:4318/v1/traces?x=1",
        "http://collector.example:4318/v1/traces?",
    ],
)
def test_a_query_over_https_loopback_in_cluster_or_empty_is_fine(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    assert tracing.setup(exporter=InMemorySpanExporter()) is True
