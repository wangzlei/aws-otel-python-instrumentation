#!/bin/sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Usage: check-arch.sh <file.so> <amd64|arm64>
#
# Fails unless the ELF e_machine of the file matches the architecture (62 = x86_64, 183 = aarch64). SDK image builds
# run it on the libotelinject_<arch>.so they copy in: musl refuses to start a process whose LD_PRELOAD entry has the
# wrong architecture, so a wrong file must never reach an image. POSIX sh and od only, so it runs in minimal build
# stages.
set -eu
if [ $# -ne 2 ]; then
  echo "usage: $0 <file.so> <amd64|arm64>" >&2
  exit 2
fi
f=$1
arch=$2
case "$arch" in
  amd64) want=62 ;;
  arm64) want=183 ;;
  *) echo "unknown architecture: $arch" >&2; exit 2 ;;
esac
magic=$(od -An -tx1 -N4 "$f" | tr -d ' \n')
if [ "$magic" != "7f454c46" ]; then
  echo "$f: not an ELF file" >&2
  exit 1
fi
# e_machine is a 16-bit little-endian field at offset 18 (both supported architectures are little-endian).
machine=$(od -An -tu2 -j18 -N2 "$f" | tr -d ' \n')
if [ "$machine" != "$want" ]; then
  echo "$f: ELF machine $machine, expected $want for $arch" >&2
  exit 1
fi
