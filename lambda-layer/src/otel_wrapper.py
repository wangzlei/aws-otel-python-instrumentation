"""Lambda wrapper — supports both lite SDK and full ADOT SDK modes.

Mode is controlled by OTEL_LAMBDA_COLDSTART_BOOST environment variable:
  true  → Lite SDK: minimal provider + XRay UDP exporter (low cold start)
  false → Full SDK: initialized by opentelemetry-instrument (default)
"""

import os
from importlib import import_module
from typing import Any

from opentelemetry.context import Context
from opentelemetry.propagate import get_global_textmap
from opentelemetry.propagators.aws.aws_xray_propagator import (
    AwsXRayPropagator,
    TRACE_HEADER_KEY,
)
from opentelemetry.trace import get_current_span

if os.environ.get("OTEL_LAMBDA_COLDSTART_BOOST") == "true":
    # --- Lite SDK mode ---
    from opentelemetry import trace
    from amazon.opentelemetry.distro.lite_sdk import (
        LiteTracerProvider,
        SimpleProcessor,
        XRayUdpSpanExporter,
    )

    daemon_address = os.environ.get("AWS_XRAY_DAEMON_ADDRESS", "127.0.0.1:2000")
    host, _, port = daemon_address.rpartition(":")
    port = int(port) if port else 2000
    host = host or "127.0.0.1"

    exporter = XRayUdpSpanExporter(host=host, port=port)
    processor = SimpleProcessor(exporter)
    provider = LiteTracerProvider(processor)
    trace.set_tracer_provider(provider)

    from opentelemetry.propagate import set_global_textmap
    set_global_textmap(AwsXRayPropagator())

    from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor

    BotocoreInstrumentor().instrument()
    RequestsInstrumentor().instrument()
    URLLib3Instrumentor().instrument()

# --- Lambda instrumentor (both modes) ---
from opentelemetry.instrumentation.aws_lambda import _X_AMZN_TRACE_ID, AwsLambdaInstrumentor


def custom_event_context_extractor(lambda_event: Any) -> Context:
    xray_env_var = os.environ.get(_X_AMZN_TRACE_ID)
    lambda_trace_context = AwsXRayPropagator().extract({TRACE_HEADER_KEY: xray_env_var})
    parent_span_context = get_current_span(lambda_trace_context).get_span_context()

    if parent_span_context is None or not parent_span_context.is_valid:
        headers = None
        try:
            headers = lambda_event["headers"]
        except (TypeError, KeyError):
            pass
        if not isinstance(headers, dict):
            headers = {}
        return get_global_textmap().extract(headers)

    return lambda_trace_context


AwsLambdaInstrumentor().instrument(event_context_extractor=custom_event_context_extractor)

# --- Load user's original handler ---


class HandlerError(Exception):
    pass


path = os.environ.get("ORIG_HANDLER")

if path is None:
    raise HandlerError("ORIG_HANDLER is not defined.")

try:
    (mod_name, handler_name) = path.rsplit(".", 1)
except ValueError as e:
    raise HandlerError("Bad path '{}' for ORIG_HANDLER: {}".format(path, str(e)))

modified_mod_name = ".".join(mod_name.split("/"))
handler_module = import_module(modified_mod_name)
lambda_handler = getattr(handler_module, handler_name)
