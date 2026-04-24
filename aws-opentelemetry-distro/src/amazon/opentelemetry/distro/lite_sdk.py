"""Minimal OTel SDK — implements just enough of the API to produce spans
and export them as JSON via stdlib urllib.request."""

import json
import random
import socket
import time
import threading

from opentelemetry import trace
from opentelemetry.trace import (
    Span,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
    NonRecordingSpan,
)
from opentelemetry.trace.span import TraceState
from opentelemetry import context as context_api
from opentelemetry.util.types import Attributes


def _ns() -> int:
    return int(time.time() * 1e9)


class LiteSpan(Span):
    def __init__(self, name, context, processor=None, parent=None,
                 kind=SpanKind.INTERNAL, attributes=None):
        self._name = name
        self._processor = processor
        self._context = context
        self._parent = parent
        self._kind = kind
        self._attributes = dict(attributes) if attributes else {}
        self._events = []
        self._status = Status(StatusCode.UNSET)
        self._start_time = _ns()
        self._end_time = None
        self._recording = True

    def get_span_context(self):
        return self._context

    def set_attribute(self, key, value):
        if self._recording:
            self._attributes[key] = value

    def set_attributes(self, attributes):
        if self._recording:
            self._attributes.update(attributes)

    def add_event(self, name, attributes=None, timestamp=None):
        if self._recording:
            self._events.append({
                "name": name,
                "attributes": dict(attributes) if attributes else {},
                "timestamp": timestamp or _ns(),
            })

    def set_status(self, status, description=None):
        if self._recording:
            if isinstance(status, Status):
                self._status = status
            else:
                self._status = Status(status, description)

    def update_name(self, name):
        self._name = name

    def is_recording(self):
        return self._recording

    def end(self, end_time=None):
        if not self._recording:
            return
        self._end_time = end_time or _ns()
        self._recording = False
        if self._processor:
            self._processor.on_end(self)

    def record_exception(self, exception, attributes=None, timestamp=None, escaped=False):
        self.add_event("exception", {
            "exception.type": type(exception).__qualname__,
            "exception.message": str(exception),
            **(attributes or {}),
        }, timestamp)

    def to_dict(self):
        ctx = self._context
        parent_id = None
        if self._parent and hasattr(self._parent, 'span_id'):
            parent_id = f"{self._parent.span_id:016x}"

        return {
            "name": self._name,
            "trace_id": f"{ctx.trace_id:032x}",
            "span_id": f"{ctx.span_id:016x}",
            "parent_span_id": parent_id,
            "kind": self._kind.name if hasattr(self._kind, 'name') else str(self._kind),
            "start_time_ns": self._start_time,
            "end_time_ns": self._end_time,
            "attributes": self._attributes,
            "events": self._events,
            "status": {
                "code": self._status.status_code.name,
                "description": self._status.description,
            },
        }


class LiteTracer(trace.Tracer):
    def __init__(self, name, provider):
        self._name = name
        self._provider = provider

    def start_span(self, name, context=None, kind=SpanKind.INTERNAL,
                   attributes=None, links=None, start_time=None,
                   record_exception=True, set_status_on_exception=True):
        parent_ctx = context or context_api.get_current()
        parent_span = trace.get_current_span(parent_ctx)
        parent_sc = parent_span.get_span_context() if parent_span else None

        if parent_sc and parent_sc.is_valid:
            trace_id = parent_sc.trace_id
        else:
            trace_id = random.getrandbits(128)

        span_ctx = SpanContext(
            trace_id=trace_id,
            span_id=random.getrandbits(64),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
            trace_state=TraceState(),
        )

        span = LiteSpan(
            name=name,
            context=span_ctx,
            processor=self._provider._processor,
            parent=parent_sc if parent_sc and parent_sc.is_valid else None,
            kind=kind,
            attributes=attributes,
        )
        if start_time:
            span._start_time = start_time
        return span

    def start_as_current_span(self, name, context=None, kind=SpanKind.INTERNAL,
                              attributes=None, links=None, start_time=None,
                              record_exception=True, set_status_on_exception=True,
                              end_on_exit=True):
        span = self.start_span(
            name, context=context, kind=kind, attributes=attributes,
            links=links, start_time=start_time,
        )
        return trace.use_span(
            span, end_on_exit=end_on_exit,
            record_exception=record_exception,
            set_status_on_exception=set_status_on_exception,
        )



def _span_to_xray_segment(span):
    """Translate a LiteSpan to an X-Ray segment/subsegment document."""
    ctx = span._context
    trace_id_hex = f"{ctx.trace_id:032x}"
    # X-Ray trace ID: 1-{8hex epoch}-{24hex random}
    xray_trace_id = f"1-{trace_id_hex[:8]}-{trace_id_hex[8:]}"

    span_id = f"{ctx.span_id:016x}"
    start_time = span._start_time / 1e9
    end_time = span._end_time / 1e9 if span._end_time else None

    parent_id = None
    if span._parent and hasattr(span._parent, "span_id"):
        parent_id = f"{span._parent.span_id:016x}"

    # Segment vs subsegment: SERVER/CONSUMER with no parent → segment, otherwise subsegment
    is_subsegment = not (
        span._kind in (SpanKind.SERVER, SpanKind.CONSUMER) and parent_id is None
    )

    attrs = span._attributes

    # Resolve name: rpc.service > service name > span name
    name = attrs.get("rpc.service") or span._name
    name = name[:200]

    segment = {
        "name": name,
        "id": span_id,
        "trace_id": xray_trace_id,
        "start_time": start_time,
    }

    if end_time:
        segment["end_time"] = end_time
    else:
        segment["in_progress"] = True

    if parent_id:
        segment["parent_id"] = parent_id

    if is_subsegment:
        segment["type"] = "subsegment"
        if span._kind in (SpanKind.CLIENT, SpanKind.PRODUCER):
            if attrs.get("rpc.system") == "aws-api":
                segment["namespace"] = "aws"
            else:
                segment["namespace"] = "remote"

    # HTTP
    http_data = {}
    req = {}
    if "http.method" in attrs:
        req["method"] = attrs["http.method"]
    if "http.url" in attrs:
        req["url"] = attrs["http.url"]
    if "http.user_agent" in attrs:
        req["user_agent"] = attrs["http.user_agent"]
    if req:
        http_data["request"] = req
    resp = {}
    if "http.status_code" in attrs:
        resp["status"] = attrs["http.status_code"]
    if resp:
        http_data["response"] = resp
    if http_data:
        segment["http"] = http_data

    # AWS metadata
    aws_data = {}
    if "cloud.account.id" in attrs:
        aws_data["account_id"] = attrs["cloud.account.id"]
    if "cloud.region" in attrs or "aws.region" in attrs:
        aws_data["region"] = attrs.get("cloud.region") or attrs.get("aws.region")
    if "rpc.method" in attrs:
        aws_data["operation"] = attrs["rpc.method"]
    if "aws.request_id" in attrs:
        aws_data["request_id"] = attrs["aws.request_id"]
    if "retry_attempts" in attrs:
        aws_data["retries"] = attrs["retry_attempts"]
    if aws_data:
        segment["aws"] = aws_data

    # Error/fault from status
    status_code = span._status.status_code
    if status_code == StatusCode.ERROR:
        http_status = attrs.get("http.status_code", 500)
        if isinstance(http_status, int) and 400 <= http_status < 500:
            segment["error"] = True
            if http_status == 429:
                segment["throttle"] = True
        else:
            segment["fault"] = True

    # Exceptions → cause
    exception_events = [e for e in span._events if e["name"] == "exception"]
    if exception_events:
        exceptions = []
        for ev in exception_events:
            exc = {}
            if "exception.type" in ev["attributes"]:
                exc["type"] = ev["attributes"]["exception.type"]
            if "exception.message" in ev["attributes"]:
                exc["message"] = ev["attributes"]["exception.message"]
            if exc:
                exc["id"] = f"{random.getrandbits(64):016x}"
                exceptions.append(exc)
        if exceptions:
            segment["cause"] = {"exceptions": exceptions}

    # Annotations: primitive types from non-standard attributes
    _SKIP_KEYS = {
        "rpc.system", "rpc.service", "rpc.method",
        "http.method", "http.url", "http.status_code", "http.user_agent",
        "cloud.account.id", "cloud.region", "cloud.resource_id",
        "aws.region", "aws.request_id", "retry_attempts",
        "server.address", "server.port",
        "faas.invocation_id", "code.function.name", "code.file.path", "code.line.number",
    }
    annotations = {}
    metadata = {}
    for k, v in attrs.items():
        if k in _SKIP_KEYS:
            continue
        if isinstance(v, (str, int, float, bool)):
            annotations[k.replace(".", "_")] = v
        else:
            metadata.setdefault("default", {})[k.replace(".", "_")] = v
    if annotations:
        segment["annotations"] = annotations
    if metadata:
        segment["metadata"] = metadata

    return segment


_XRAY_HEADER = '{"format":"json","version":1}\n'


class XRayUdpSpanExporter:
    """Exports spans as X-Ray segment documents over UDP to the X-Ray daemon."""

    def __init__(self, host="127.0.0.1", port=2000):
        self._addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def export(self, spans):
        for span in spans:
            segment = _span_to_xray_segment(span)
            payload = (_XRAY_HEADER + json.dumps(segment)).encode()
            try:
                self._sock.sendto(payload, self._addr)
            except Exception:
                pass

    def shutdown(self):
        self._sock.close()


class UdpSpanExporter:
    """Exports spans as JSON over UDP. Fire-and-forget, zero connection overhead."""

    def __init__(self, host="127.0.0.1", port=2000):
        self._addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def export(self, spans):
        payload = json.dumps([s.to_dict() for s in spans]).encode()
        try:
            self._sock.sendto(payload, self._addr)
        except Exception:
            pass

    def shutdown(self):
        self._sock.close()


class SimpleProcessor:
    """Collects ended spans and exports in batches or on flush."""

    def __init__(self, exporter):
        self._exporter = exporter
        self._spans = []
        self._lock = threading.Lock()

    def on_end(self, span):
        with self._lock:
            self._spans.append(span)

    def flush(self, timeout_millis=30000):
        with self._lock:
            to_export = self._spans[:]
            self._spans.clear()
        if to_export:
            for s in to_export:
                print("[lite-sdk]", json.dumps(s.to_dict(), default=str))
            self._exporter.export(to_export)

    def shutdown(self):
        self.flush()
        self._exporter.shutdown()


class LiteTracerProvider(trace.TracerProvider):
    def __init__(self, processor=None):
        self._processor = processor

    def get_tracer(self, instrumenting_module_name="", instrumenting_library_version="",
                   schema_url=None, attributes=None):
        return LiteTracer(instrumenting_module_name, self)

    def add_span_processor(self, processor):
        self._processor = processor

    def force_flush(self, timeout_millis=30000):
        if self._processor:
            self._processor.flush(timeout_millis)

    def shutdown(self):
        if self._processor:
            self._processor.shutdown()
