# This Docker file builds auto-instrumentation image for aws-otel-python-instrumentation following upstream Docker build instruction:
# https://github.com/open-telemetry/opentelemetry-operator/blob/main/autoinstrumentation/python/Dockerfile
#   The packages are installed in the `/autoinstrumentation` directory. This is required as when instrumenting the pod by CWOperator,
#   one init container will be created to copy all the content in `/autoinstrumentation` directory to app's container. Then
#   update the `PYTHONPATH` environment variable accordingly. Then in the second stage, copy the directory to `/autoinstrumentation`.

# Stage 1: Install ADOT Python in the /operator-build folder
FROM public.ecr.aws/docker/library/python:3.11 AS build

WORKDIR /operator-build

ADD aws-opentelemetry-distro/ ./aws-opentelemetry-distro/

# Remove opentelemetry-exporter-otlp-proto-grpc and grpcio, as grpcio has strict dependencies on the Python version and
# will cause confusing failures if gRPC protocol is used. Now if gRPC protocol is requested by the user, instrumentation
# will complain that grpc is not installed, which is more understandable. References:
# * https://github.com/open-telemetry/opentelemetry-operator/blob/b5bb0ae34720d4be2d229dafecb87b61b37699b0/autoinstrumentation/python/requirements.txt#L2
# * https://github.com/MicrosoftDocs/azure-docs/blob/main/articles/azure-functions/recover-python-functions.md#troubleshoot-cannot-import-cygrpc
RUN sed -i "/opentelemetry-exporter-otlp-proto-grpc/d" ./aws-opentelemetry-distro/pyproject.toml

RUN mkdir workspace && pip install --target workspace ./aws-opentelemetry-distro

# Replace the upstream auto_instrumentation sitecustomize.py with the start-up guard version, which runs the same
# initialize() and additionally reports to the OTel Injector how the run went (inert without the Injector, so the
# operator's current injection behaves exactly as before). Fail the build if upstream changed the file, so that its new
# content is reviewed and carried over instead of silently dropped.
RUN set -e; \
    target=workspace/opentelemetry/instrumentation/auto_instrumentation/sitecustomize.py; \
    grep -qx "from opentelemetry.instrumentation.auto_instrumentation import initialize" "$target"; \
    [ "$(grep -cv '^\(#.*\)\?$' "$target")" = "2" ]; \
    grep -qx "initialize()" "$target"; \
    cp workspace/amazon/opentelemetry/distro/startup_guard_sitecustomize.py "$target"; \
    rm -f workspace/opentelemetry/instrumentation/auto_instrumentation/__pycache__/sitecustomize.*.pyc

# The upstream OTel Injector looks for the Python SDK at <prefix>/glibc or <prefix>/musl. These two self-referencing
# links make the same files available at those paths, so one image serves both today's operator injection (which uses
# /autoinstrumentation directly) and the Injector, without shipping the SDK twice. COPY and the image's cp
# (tools/cp-utility) keep them as links.
RUN ln -s . workspace/glibc && ln -s . workspace/musl

# Stage 2: Download the pinned OTel Injector release (built in tools/otel-injector of this repository) and verify it.
# Both architectures go into every image; cp-utility installs only the one matching the node.
# The last two checks read the ELF e_machine field (offset 18, little endian): 62 = x86_64, 183 = aarch64.
# TODO: the defaults are the development release; set them to the first official aws-otel-injector-v* release.
FROM --platform=$BUILDPLATFORM public.ecr.aws/docker/library/alpine:3.22 AS otel-injector
ARG OTEL_INJECTOR_RELEASE_BASE_URL=https://github.com/aws-observability/aws-otel-python-instrumentation/releases/download
ARG OTEL_INJECTOR_VERSION=0.11.0-1
ARG OTEL_INJECTOR_SHA256_AMD64=c9ea9b2051d70ead7cca7155664c8dd927fc111b9b2e711795f95b985b03f4e0
ARG OTEL_INJECTOR_SHA256_ARM64=879219069478636c9c2632cf6816076eb084d0b25fd5c0cb1adfbc25a9c26e34
WORKDIR /out
RUN set -e; \
    base="${OTEL_INJECTOR_RELEASE_BASE_URL}/aws-otel-injector-v${OTEL_INJECTOR_VERSION}"; \
    wget -q -O libotelinject_amd64.so "${base}/libotelinject_amd64.so"; \
    wget -q -O libotelinject_arm64.so "${base}/libotelinject_arm64.so"; \
    echo "${OTEL_INJECTOR_SHA256_AMD64}  libotelinject_amd64.so" | sha256sum -c -; \
    echo "${OTEL_INJECTOR_SHA256_ARM64}  libotelinject_arm64.so" | sha256sum -c -; \
    [ "$(od -An -j18 -N2 -tu2 libotelinject_amd64.so | tr -d ' ')" = "62" ]; \
    [ "$(od -An -j18 -N2 -tu2 libotelinject_arm64.so | tr -d ' ')" = "183" ]

# Stage 3: Build the cp-utility binary
FROM public.ecr.aws/docker/library/rust:1.89 AS builder

WORKDIR /usr/src/cp-utility
COPY ./tools/cp-utility .

## TARGETARCH is defined by buildx
# https://docs.docker.com/engine/reference/builder/#automatic-platform-args-in-the-global-scope
ARG TARGETARCH

# Run validations and audit only on amd64 because it is faster and those two steps
# are only used to validate the source code and don't require anything that is
# architecture specific.

# Validations
# Validate formatting
RUN if [ $TARGETARCH = "amd64" ]; then rustup component add rustfmt && cargo fmt --check ; fi

# Audit dependencies
RUN if [ $TARGETARCH = "amd64" ]; then cargo install cargo-audit --locked && cargo audit ; fi


# Cross-compile based on the target platform.
RUN if [ $TARGETARCH = "amd64" ]; then export ARCH="x86_64" ; \
    elif [ $TARGETARCH = "arm64" ]; then export ARCH="aarch64" ; \
    else false; \
    fi \
    && rustup target add ${ARCH}-unknown-linux-musl \
    && cargo test  --target ${ARCH}-unknown-linux-musl \
    && cargo install --target ${ARCH}-unknown-linux-musl --path . --root .

# Stage 4: Build the distribution image by copying the THIRD-PARTY-LICENSES, the custom built cp command from stage 3, the installed ADOT Python from stage 1 and the OTel Injector from stage 2 to their respective destinations
FROM scratch

# Required to copy attribute files to distributed docker images
ADD THIRD-PARTY-LICENSES ./THIRD-PARTY-LICENSES

COPY --from=builder /usr/src/cp-utility/bin/cp-utility /bin/cp
COPY --from=build /operator-build/workspace /autoinstrumentation
COPY --from=otel-injector /out /autoinstrumentation/injector
