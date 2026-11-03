# ADOT build of the OpenTelemetry Injector

This directory builds `libotelinject.so`, the [OpenTelemetry Injector](https://github.com/open-telemetry/opentelemetry-injector)
with a small set of ADOT patches, for x86_64 and aarch64 Linux. The library is copied into the ADOT
auto-instrumentation images of all four languages (Python, Java, Node.js, .NET); on Kubernetes the operator loads it
into the application container with `LD_PRELOAD`. Before the application's `main()` and before the language runtime
starts, it decides whether to inject the ADOT SDK and sets the environment variables the SDK needs.

The main addition is a **start-up guard**: when the SDK keeps an application from starting, the next restarts of that
container start the application without the SDK, so it recovers in place. See [SPEC.md](SPEC.md) for the contract
between the Injector and the SDKs (state files, rules, constants, per-language hooks).

Customers never use these files directly; they ship inside the SDK images.

## Layout

| Path | Purpose |
|---|---|
| `UPSTREAM` | Upstream release (tag, tarball URL, SHA256) and the Zig toolchain it requires |
| `patches/` | ADOT patches, applied in order on top of the upstream release |
| `build.sh` | Downloads and verifies upstream, applies the patches, runs the upstream unit tests, builds both architectures |
| `check-arch.sh` | Checks the ELF architecture of a `.so`; used by `build.sh` and by the SDK image builds |
| `bump-upstream.sh` | Points `UPSTREAM` at a newer upstream release (used by the weekly upstream bump workflow) |
| `SPEC.md` | Injector ↔ SDK contract |

### Patches

| Patch | What it does |
|---|---|
| `0001-startup-guard.patch` | The start-up guard (`src/adot_guard.zig`), with unit tests |
| `0002-python-pythonpath.patch` | `PYTHONPATH` in the same order the Kubernetes operators use today |
| `0003-quiet-unconfigured-runtimes.patch` | Debug-level logging for runtimes without an agent path and missing libc builds (the injector runs in every process) |
| `0004-agent-in-prefix-layout.patch` | Python and .NET: use an SDK laid out directly in the agent path prefix (today's image layout) |

The patches are kept in upstream style (one change each, with tests) so they can be proposed upstream.

## Building

```bash
tools/otel-injector/build.sh out/
# out/libotelinject_amd64.so  out/libotelinject_arm64.so  out/SHA256SUMS
```

Needs `bash`, `curl`, `tar` (with xz), `patch` and `sha256sum`. `build.sh` downloads the Zig toolchain pinned in
`UPSTREAM`, verifies its SHA256 and keeps it in `/tmp/adot-otel-injector-zig`; it always uses that toolchain, never
one from `PATH`, and builds in `/tmp/adot-otel-injector-build`, so that the output is reproducible. It runs
`zig fmt --check` and the full upstream test suite (including the guard's tests) before building.

## Releases

The Injector is released on its own, independent of the Python SDK release. When it changed (a patch, a new
upstream version), run the `OTel Injector Release` workflow manually. It builds the library at that commit and
creates a GitHub release with `libotelinject_amd64.so`, `libotelinject_arm64.so`, `SHA256SUMS` and a build
provenance attestation, not marked as the repository's latest release.

Tags follow the upstream version in `UPSTREAM`: `aws-otel-injector-v<upstream version>` (e.g.
`aws-otel-injector-v0.11.0`) for the first release on an upstream version, then `aws-otel-injector-v0.11.0-1`,
`-2`, … when only the ADOT patches changed. A new upstream version starts again without a suffix. The workflow takes
the version from `UPSTREAM`; its only input is the suffix, and it refuses a tag that already exists. These tags are
separate from the SDK's own `v<version>` tags.

The build is reproducible (same files for the same commit), so `SHA256SUMS` identifies the binaries.

## How SDK repositories consume the Injector

All four SDK repositories (Python included, like the others) treat the Injector as a pinned external artifact:

1. **Pin**: the image build pins one `aws-otel-injector-v*` release, with the contents of its `SHA256SUMS`.
2. **Verify**: it downloads `libotelinject_amd64.so` and `libotelinject_arm64.so`, checks them against the pinned
   checksums, and runs `check-arch.sh` on each (`libotelinject_amd64.so amd64`, `libotelinject_arm64.so arm64`).
3. **Copy**: both files go into `/autoinstrumentation/injector/` of every architecture's image. The image's `cp`
   (cp-utility) installs only the file matching the node's architecture as `libotelinject.so` when the operator's
   init container copies the SDK. The image keeps today's `/autoinstrumentation` layout, so the old injection mode
   and older operators keep working.
4. **Update**: each repository has a small weekly workflow that opens a PR bumping the pin (version and checksums)
   when a newer `aws-otel-injector-v*` release exists. "Newer" means a later publish date, not a higher semantic
   version: in semver `0.11.0-1` sorts before `0.11.0`. Releases are cut only when something changed, so a newer
   release always means new binaries.

## Upstream updates

The `OTel Injector Upstream Bump` workflow runs weekly (Tuesdays) and on demand. When upstream has a newer release
it updates `UPSTREAM` (tag, tarball SHA256, and the Zig toolchain if the new release needs another one) with
`bump-upstream.sh`, runs `build.sh`, and opens a PR. If the patches no longer apply or the tests fail, the PR is a
draft with the build log, for a maintainer to rebase the patches (below). Merging the PR does not release anything;
release the Injector afterwards.

## Updating upstream / dropping a patch

1. Change `UPSTREAM` to the new tag, tarball and SHA256, and the Zig version its `zig-version` file requires.
2. Apply the patches to the new release (`git am patches/*.patch` in an upstream checkout at that tag), resolve
   conflicts, and regenerate them with `git format-patch --no-signature --zero-commit`.
3. If upstream has absorbed a change, delete that patch file and renumber the rest.
4. Run `build.sh`; it must pass the upstream tests and the guard's tests.
5. Release `aws-otel-injector-v<new upstream version>`; the SDK repositories' weekly bump workflows pick it up.

Any change to the Injector ↔ SDK contract must keep it backward and forward compatible (see SPEC.md,
"Compatibility").
