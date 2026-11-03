#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Builds the ADOT build of the OpenTelemetry Injector: the upstream release in UPSTREAM plus patches/*.patch,
# for x86_64 and aarch64.
#
# Usage: tools/otel-injector/build.sh <output dir>
#
# Output: <output dir>/libotelinject_amd64.so, <output dir>/libotelinject_arm64.so, <output dir>/SHA256SUMS
#
# Needs bash, curl, tar (with xz), patch, sha256sum. Downloads and verifies the Zig toolchain pinned in UPSTREAM
# into /tmp/adot-otel-injector-zig/<version>-<sha256> (kept between runs).
#
# The build is reproducible: the binaries embed debug information with source, toolchain and cache paths, so the
# toolchain always comes from that fixed path (never from PATH) and the sources are built in a fixed directory,
# /tmp/adot-otel-injector-build (recreated on every run), with the Zig caches inside it. Builds on hosts of the same
# architecture produce identical files, so consumers can tell from SHA256SUMS whether a release changed anything.
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $0 <output dir>" >&2
  exit 2
fi

here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$1"
out=$(cd "$1" && pwd)
# shellcheck source=UPSTREAM
source "$here/UPSTREAM"

work=/tmp/adot-otel-injector-build
rm -rf "$work"
mkdir -p "$work"
trap 'rm -rf "$work"' EXIT
export ZIG_GLOBAL_CACHE_DIR=$work/zig-global-cache ZIG_LOCAL_CACHE_DIR=$work/zig-local-cache

fetch() { # fetch <url> <sha256> <file>
  curl --fail --silent --show-error --location --retry 3 -o "$3" "$1"
  echo "$2  $3" | sha256sum --check --quiet
}

zig_bin() {
  local url sha
  case "$(uname -m)" in
    x86_64) url=$ZIG_X86_64_LINUX_URL sha=$ZIG_X86_64_LINUX_SHA256 ;;
    aarch64 | arm64) url=$ZIG_AARCH64_LINUX_URL sha=$ZIG_AARCH64_LINUX_SHA256 ;;
    *) echo "unsupported build host architecture: $(uname -m)" >&2; exit 1 ;;
  esac
  local cache=/tmp/adot-otel-injector-zig
  local dir=$cache/$ZIG_VERSION-$sha
  if [ ! -x "$dir/zig" ]; then
    mkdir -p "$cache"
    fetch "$url" "$sha" "$work/zig.tar.xz"
    rm -rf "$dir.tmp" && mkdir -p "$dir.tmp"
    tar -C "$dir.tmp" --strip-components=1 -xJf "$work/zig.tar.xz"
    rm -rf "$dir" && mv "$dir.tmp" "$dir"
  fi
  echo "$dir/zig"
}

zig=$(zig_bin)
echo "zig: $zig ($("$zig" version))"

fetch "$UPSTREAM_TARBALL" "$UPSTREAM_SHA256" "$work/src.tar.gz"
mkdir "$work/src"
tar -C "$work/src" --strip-components=1 -xzf "$work/src.tar.gz"
for p in "$here"/patches/*.patch; do
  echo "applying $(basename "$p")"
  patch -d "$work/src" -p1 --forward --silent --no-backup-if-mismatch < "$p"
done

cd "$work/src"
"$zig" fmt --check src
"$zig" build test --summary all
for arch in amd64 arm64; do
  "$zig" build -Dcpu-arch="$arch" --summary none
  cp so/libotelinject.so "$out/libotelinject_$arch.so"
  "$here/check-arch.sh" "$out/libotelinject_$arch.so" "$arch"
done

cd "$out"
sha256sum libotelinject_amd64.so libotelinject_arm64.so > SHA256SUMS
cat SHA256SUMS
