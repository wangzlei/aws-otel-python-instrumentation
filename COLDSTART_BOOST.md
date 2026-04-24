# Lambda Cold Start Boost — Implementation Guide

## Problem

OpenTelemetry adds significant cold start overhead to Lambda functions. On a 512MB Python Lambda, the full ADOT SDK adds **+632ms (83%)** to Init Duration. The root cause is **module imports at startup** — not instrumentation or monkey patching (<2ms).

The heaviest import chains:

| Dependency | Why loaded |
|-----------|-----------|
| `protobuf` (descriptor pool) | OTLP serialization |
| `requests` + `urllib3` + `charset_normalizer` | OTLP HTTP transport |
| `opentelemetry.sdk.resources` (pulls in `email.*`) | Resource detection |
| `opentelemetry.sdk.metrics` | Transitive — loaded even when only tracing |
| `opentelemetry.sdk.trace` + `BatchSpanProcessor` | Full SDK trace pipeline |

## Solution

Replace the full OTel SDK with a **Lite SDK** (~300 lines) when `OTEL_LAMBDA_COLDSTART_BOOST=true`:

1. Implements OTel API interfaces (`TracerProvider`, `Tracer`, `Span`) — compatible with all instrumentation libraries
2. Translates spans to **X-Ray segment format** (no protobuf)
3. Sends via **UDP** to X-Ray daemon on localhost:2000 (stdlib `socket`, no `requests`)
4. Bypasses `opentelemetry-instrument` CLI entirely — avoids loading the heavy configurator module

```
                        Full SDK path (default)
                        ┌─────────────────────────────────────────┐
                        │ opentelemetry-instrument                │
                        │   → load configurator (50+ imports)     │
                        │   → TracerProvider + BatchSpanProcessor │
                        │   → OTLP HTTP exporter (protobuf)      │
                        │   → load instrumentors                  │
                        └─────────────────────────────────────────┘

                        Boost path (OTEL_LAMBDA_COLDSTART_BOOST=true)
                        ┌─────────────────────────────────────────┐
                        │ exec "$@" (skip opentelemetry-instrument)│
                        │   → LiteTracerProvider (~0ms)           │
                        │   → XRayUdpSpanExporter (stdlib socket) │
                        │   → load instrumentors                  │
                        └─────────────────────────────────────────┘
```

## Results (512MB Python Lambda, P50 of 5 cold starts)

| Configuration | Init Duration | OTel Overhead |
|--------------|---------------|---------------|
| No OTel (baseline) | 762ms | — |
| **Boost mode (Lite SDK)** | **975ms** | **+212ms (+28%)** |
| Full SDK mode | 1,478ms | +716ms (+94%) |
| Original ADOT layer | 1,394ms | +632ms (+83%) |

**Boost mode eliminates 66% of OTel cold start overhead.**

Results are consistent across memory sizes:

| Memory | Baseline | Lite SDK | Original ADOT | Overhead Reduction |
|--------|----------|----------|---------------|--------------------|
| 256MB | 781ms | 1,007ms (+226ms) | 1,330ms (+549ms) | 59% |
| 512MB | 762ms | 975ms (+212ms) | 1,394ms (+632ms) | 66% |
| 1024MB | 764ms | 1,018ms (+254ms) | 1,386ms (+626ms) | 60% |

## Architecture

### Files changed (ADOT repo)

```
aws-otel-python-instrumentation/
├── aws-opentelemetry-distro/src/amazon/opentelemetry/distro/
│   ├── lite_sdk.py                          # NEW — Lite SDK implementation
│   └── aws_opentelemetry_configurator.py    # MODIFIED — Lambda lite path
└── lambda-layer/src/
    ├── otel-instrument                      # MODIFIED — mode switch
    └── otel_wrapper.py                      # MODIFIED — mode switch
```

### `otel-instrument` (shell wrapper)

Checks `OTEL_LAMBDA_COLDSTART_BOOST`:
- `true` → `exec "$@"` (pass through to Lambda runtime, skip `opentelemetry-instrument`)
- default → `exec python3 ... opentelemetry-instrument "$@"` (original full SDK path)

Both modes set `_HANDLER=otel_wrapper.lambda_handler` to wrap the user's handler.

### `otel_wrapper.py` (Python wrapper)

Checks `OTEL_LAMBDA_COLDSTART_BOOST`:
- `true` → imports `lite_sdk`, sets up `LiteTracerProvider` + `XRayUdpSpanExporter`, manually instruments botocore/requests/urllib3
- default → relies on full SDK already initialized by `opentelemetry-instrument`

Both modes run `AwsLambdaInstrumentor` for the Lambda handler span.

### `lite_sdk.py` (~300 lines)

| Class | Replaces | What it does |
|-------|----------|-------------|
| `LiteTracerProvider` | `opentelemetry.sdk.trace.TracerProvider` | Returns `LiteTracer`, supports `force_flush` |
| `LiteTracer` | `opentelemetry.sdk.trace.Tracer` | Creates `LiteSpan`, manages parent context |
| `LiteSpan` | `opentelemetry.sdk.trace.ReadableSpan` | Full `Span` interface, calls `processor.on_end()` |
| `SimpleProcessor` | `BatchSpanProcessor` | Collects spans, exports on flush |
| `XRayUdpSpanExporter` | `OTLPSpanExporter` | Translates to X-Ray segment JSON, sends via UDP |

### Span → X-Ray segment translation

| OTel | X-Ray |
|------|-------|
| `trace_id` (32 hex) | `1-{first 8 hex}-{remaining 24 hex}` |
| `span_id` (16 hex) | `id` |
| `parent_span_id` | `parent_id` |
| SERVER/CONSUMER (no parent) | Segment |
| Everything else | Subsegment (`type: "subsegment"`) |
| `start_time` (ns) | `start_time` (float seconds) |
| `rpc.system=aws-api` | `namespace: "aws"` |
| CLIENT/PRODUCER | `namespace: "remote"` |
| `rpc.service` | segment `name` |
| `rpc.method` | `aws.operation` |
| `http.method/url/status_code` | `http.request/response` |
| `cloud.region` | `aws.region` |
| `aws.request_id` | `aws.request_id` |
| StatusCode.ERROR + 4xx | `error: true` |
| StatusCode.ERROR + 5xx | `fault: true` |
| Exception events | `cause.exceptions[]` |

### What boost mode preserves

- Full compatibility with OTel instrumentation libraries (botocore, requests, urllib3, etc.)
- W3C TraceContext and X-Ray context propagation
- Parent-child span relationships
- X-Ray trace visibility (segments sent via UDP to daemon)

### What boost mode drops

- Resource detection (service.name, host, etc.)
- Sampling strategies (always samples)
- SpanLimits (no attribute count limits)
- Metrics and Logs
- Retry/backpressure on export

## How to test

### Prerequisites

- AWS account with Lambda permissions
- Docker (for building the layer)
- Terraform (for deploying)
- ADOT repo checked out

### Build and deploy

```bash
cd aws-otel-python-instrumentation/lambda-layer

# Login to ECR (needed for build images)
aws ecr-public get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin public.ecr.aws

# Build layer + sample app + deploy
bash build.sh
```

### Run A/B test

The test script cycles through all configurations, forcing cold starts by updating environment variables:

```bash
FUNC="aws-opentelemetry-distro-python"
REGION="us-west-2"
NEW_LAYER="<your layer ARN from terraform output>"
ORIG_LAYER="arn:aws:lambda:us-west-2:615299751070:layer:AWSOpenTelemetryDistroPython:28"
RUNS=5

for MODE in baseline lite full original; do
  echo "=== $MODE ==="
  for i in $(seq 1 $RUNS); do
    case $MODE in
      baseline)
        aws lambda update-function-configuration \
          --function-name $FUNC --region $REGION \
          --layers "$NEW_LAYER" \
          --environment "Variables={RUN=${MODE}_$i}" ;;
      lite)
        aws lambda update-function-configuration \
          --function-name $FUNC --region $REGION \
          --layers "$NEW_LAYER" \
          --environment "Variables={AWS_LAMBDA_EXEC_WRAPPER=/opt/otel-instrument,OTEL_LAMBDA_COLDSTART_BOOST=true,RUN=${MODE}_$i}" ;;
      full)
        aws lambda update-function-configuration \
          --function-name $FUNC --region $REGION \
          --layers "$NEW_LAYER" \
          --environment "Variables={AWS_LAMBDA_EXEC_WRAPPER=/opt/otel-instrument,RUN=${MODE}_$i}" ;;
      original)
        aws lambda update-function-configuration \
          --function-name $FUNC --region $REGION \
          --layers "$ORIG_LAYER" \
          --environment "Variables={AWS_LAMBDA_EXEC_WRAPPER=/opt/otel-instrument,RUN=${MODE}_$i}" ;;
    esac

    sleep 8
    aws lambda wait function-updated --function-name $FUNC --region $REGION

    # Invoke and extract Init Duration
    aws lambda invoke --function-name $FUNC --region $REGION \
      --log-type Tail --payload '{}' /tmp/out.json 2>&1 | \
      python3 -c "
import sys,json,base64,re
resp=json.load(sys.stdin)
logs=base64.b64decode(resp.get('LogResult','')).decode()
for l in logs.split('\n'):
  m=re.search(r'Init Duration: ([\d.]+)',l)
  if m: print(f'  Run $i: {m.group(1)}ms')
"
  done
done
```

### Verify X-Ray trace

After invoking with boost mode, check X-Ray console for the trace ID printed in the REPORT line:
```
XRAY TraceId: 1-69eb082b-7c56b31a26acc3d4194fd468
```

You should see the Lambda handler segment with S3.ListBuckets and HTTP GET subsegments.

## Porting to Node.js

The same approach applies. Key differences:

| Aspect | Python | Node.js |
|--------|--------|---------|
| OTel API | `opentelemetry-api` | `@opentelemetry/api` |
| Context mechanism | `opentelemetry.context` (importlib_metadata) | `AsyncLocalStorage` (built-in, fast) |
| Span serialization | `json.dumps` (stdlib) | `JSON.stringify` (built-in) |
| UDP transport | `socket` (stdlib) | `dgram` (built-in) |
| Heavy deps to skip | `protobuf`, `requests`, `sdk.resources`(email.*) | `protobufjs` (runtime compilation), `@grpc/grpc-js` |
| Expected savings | ~60% of OTel overhead | ~80% (protobufjs is heavier than Python protobuf) |

Implementation steps:
1. Create `lite-sdk.js` implementing `TracerProvider`, `Tracer`, `Span` from `@opentelemetry/api`
2. Create `XRayUdpExporter` using `dgram.createSocket('udp4')` — same X-Ray segment JSON format
3. Modify the Lambda layer wrapper to bypass `@opentelemetry/auto-instrumentations-node` when boost is enabled
4. Manually register instrumentations needed (e.g., `@opentelemetry/instrumentation-aws-sdk`)
