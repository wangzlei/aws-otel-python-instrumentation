#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Points UPSTREAM (and the THIRD-PARTY-LICENSES entry) at a newer OpenTelemetry Injector release.
#
# Usage: tools/otel-injector/bump-upstream.sh [<tag>]
#
# Without <tag>, uses the latest upstream GitHub release (needs the gh CLI and a token in GH_TOKEN). Writes the new
# tag, tarball SHA256 and, if the release needs another Zig version, the Zig toolchain URLs and SHA256s. Prints the
# new tag on stdout. Exit codes: 0 updated, 3 already up to date (or <tag> is not newer), other = error.
# It does not build; run build.sh afterwards (the patches may need rebasing).
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo_root=$(cd "$here/../.." && pwd)
upstream_file=$here/UPSTREAM
licenses_file=$repo_root/THIRD-PARTY-LICENSES
# shellcheck source=UPSTREAM
source "$upstream_file"

upstream_slug=${UPSTREAM_REPO#https://github.com/}
new_tag=${1:-}
if [ -z "$new_tag" ]; then
  new_tag=$(gh release view --repo "$upstream_slug" --json tagName --jq .tagName)
fi
case "$new_tag" in
  v[0-9]*.[0-9]*.[0-9]*) ;;
  *) echo "unexpected upstream tag: $new_tag" >&2; exit 1 ;;
esac

newest=$(printf '%s\n%s\n' "$UPSTREAM_TAG" "$new_tag" | sort -V | tail -n 1)
if [ "$new_tag" = "$UPSTREAM_TAG" ] || [ "$newest" != "$new_tag" ]; then
  echo "UPSTREAM is at $UPSTREAM_TAG; $new_tag is not newer" >&2
  exit 3
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

tarball=$UPSTREAM_REPO/archive/refs/tags/$new_tag.tar.gz
curl --fail --silent --show-error --location --retry 3 -o "$work/src.tar.gz" "$tarball"
tarball_sha=$(sha256sum "$work/src.tar.gz" | cut -d' ' -f1)

# The Zig version the release requires, from its zig-version file (ZIG_VERSION=x.y.z).
zig_version=$(tar -xzOf "$work/src.tar.gz" --wildcards '*/zig-version' | sed -n 's/^ZIG_VERSION=//p' | tr -d '[:space:]')
if [ -z "$zig_version" ]; then
  echo "cannot read the Zig version from $new_tag/zig-version" >&2
  exit 1
fi

set_key() { # set_key <key> <value>
  sed -i "s|^$1=.*|$1=$2|" "$upstream_file"
}

set_key UPSTREAM_TAG "$new_tag"
set_key UPSTREAM_TARBALL "$tarball"
set_key UPSTREAM_SHA256 "$tarball_sha"

if [ "$zig_version" != "$ZIG_VERSION" ]; then
  curl --fail --silent --show-error --location --retry 3 -o "$work/zig-index.json" https://ziglang.org/download/index.json
  zig_field() { # zig_field <platform> <tarball|shasum>
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]][sys.argv[3]][sys.argv[4]])' \
      "$work/zig-index.json" "$zig_version" "$1" "$2"
  }
  set_key ZIG_VERSION "$zig_version"
  set_key ZIG_X86_64_LINUX_URL "$(zig_field x86_64-linux tarball)"
  set_key ZIG_X86_64_LINUX_SHA256 "$(zig_field x86_64-linux shasum)"
  set_key ZIG_AARCH64_LINUX_URL "$(zig_field aarch64-linux tarball)"
  set_key ZIG_AARCH64_LINUX_SHA256 "$(zig_field aarch64-linux shasum)"
fi

sed -i "s|^\*\* opentelemetry-injector; version [^ ]* -- |** opentelemetry-injector; version ${new_tag#v} -- |" "$licenses_file"

echo "$new_tag"
