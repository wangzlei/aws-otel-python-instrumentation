# ADOT start-up guard: contract between the Injector and the SDKs

The injector (`libotelinject.so`, loaded through `LD_PRELOAD`) decides at every process start, before any
environment variable is modified, whether to inject the ADOT SDK. The SDKs only report how a run went.
Linux only (uses `/proc` and `LD_PRELOAD`).

## Settings

One environment variable only: `AWS_INSTRUMENTATION_GUARD_ENABLED=false` turns the guard off. The guard is also off
without `KUBERNETES_SERVICE_HOST` (set by the kubelet) and without our `libotelinject.so` in `LD_PRELOAD`.
Thresholds are constants: 3 fast failures, 60 s healthy, 360 s crash-loop window, 2 crashes inside the SDK,
2 fast failures while skipped restore the SDK.

- State directory = the SDK volume holding the injector: `LD_PRELOAD` entry `<volume>/injector/libotelinject.so`
  -> `<volume>` (the emptyDir root, writable by any user; with several languages the operator points `LD_PRELOAD` at
  the first injected language's copy, order java, nodejs, python, dotnet).
- `<id>` = this container's name, from the kubelet's termination-log bind mount in `/proc/self/mountinfo` (root path
  `.../pods/<pod uid>/containers/<name>/<suffix>`; the suffix changes on every restart, the name does not); if not
  found, FNV-1a 32 of `/proc/1/cmdline`.
- The SDKs derive the same path with the same rule, and are active only when the state file holds the current boot
  id and `skipped=0` (i.e. the injector decided to inject this run).
- An SDK hook is also active only when its own language's injector agent-path variable is set in the process
  (`PYTHON_AUTO_INSTRUMENTATION_AGENT_PATH_PREFIX`, `JVM_AUTO_INSTRUMENTATION_AGENT_PATH`,
  `NODEJS_AUTO_INSTRUMENTATION_AGENT_PATH`, `DOTNET_AUTO_INSTRUMENTATION_AGENT_PATH_PREFIX`), i.e. the injector set up
  that language. In a per-language injection mode, an SDK loaded the old way (today's env vars) never writes records,
  so it cannot affect the decision for the injector-managed languages in the same container.

## Compatibility

The Injector and the SDK hooks are released separately (the SDK images pin an `aws-otel-injector-v*` release), so a pod
can combine a newer Injector with older SDK hooks or the other way round. The contract in this document (file names,
formats, fields and their meaning) must therefore stay backward and forward compatible, and every change to it must
follow these rules:

- Readers ignore fields, lines and files they do not know.
- A missing or unreadable file means "no information" and leads to the fail-open behaviour (inject; for the SDK:
  write nothing), never to a crash or a skip.
- Never change the meaning or format of an existing field or file, never reuse a removed name; add new names
  instead.
- New behaviour that needs both sides must be off until both sides support it (for example, a new file the Injector
  only acts on when present).

## Files (one writer each, fixed set, bounded)

| File | Writer | Content |
|---|---|---|
| `.adot-guard-<id>` | injector, first process of a container run | `boot= fails= pod= restored= sdkcrash= skipfails= skipped=` (< 128 bytes) |
| `.adot-guard-<id>.init` | SDK, when it finished initialising | the boot id, then seconds since node boot (`/proc/uptime`) |
| `.adot-guard-<id>.healthy` | SDK, 60 s after `.init`, unless the run is ending on an uncaught exception | the boot id |
| `.adot-guard-<id>.crash` | SDK crash hook | line 1 the boot id, line 2 `file:line ExcType` (at most 400 characters) |

Every write is a new temporary file (pid + random suffix, `O_CREAT|O_EXCL|O_NOFOLLOW`) renamed over the target; on
error the temporary file is removed. At each new container run the injector also removes temporary files of this
container left by a process killed mid-write, so the number and size of the files never grow with restarts.
No termination-log writes. If the volume is not writable, the guard turns itself off: the SDK loads as usual and
PID 1 prints one line `[adot-guard] start-up check is off: cannot write ...`.

## Decision (injector)

Off (inject as upstream would) when: not on Kubernetes, `AWS_INSTRUMENTATION_GUARD_ENABLED=false`, no libotelinject.so in `LD_PRELOAD`,
no runtime agent path configured in the process (the guard only acts for the runtimes the injector manages there:
`JVM_AUTO_INSTRUMENTATION_AGENT_PATH`, `NODEJS_AUTO_INSTRUMENTATION_AGENT_PATH`, `PYTHON_AUTO_INSTRUMENTATION_AGENT_PATH_PREFIX`,
`DOTNET_AUTO_INSTRUMENTATION_AGENT_PATH_PREFIX`, `RUBY_AUTO_INSTRUMENTATION_AGENT_PATH_PREFIX`), PID 1 is `pause`
(shareProcessNamespace), or any error (unreadable `/proc`, unwritable directory, ...).

Same run (state boot == current boot): follow the recorded decision.

New run, previous state P (missing, malformed or another `pod=` means no state: start fresh and inject):
- previous run *fast failure* = no `.healthy` for P.boot and the time from the previous run's `.init` (if present for
  P.boot, else from its start) to this start < 360 s; measuring from `.init` keeps a slow SDK start-up (e.g. the ADOT
  .NET EC2 detector waiting ~5 min for IMDS) from hiding a crash right after it
- previous run *crashed inside the SDK* = `.crash` for P.boot
- P.restored: inject, never skip again in this pod
- P.skipped: skipfails = fast failure ? +1 : 0; skipfails >= 2 means the container fails without the SDK too,
  so restore (restored=1, inject); otherwise keep skipping
- otherwise: fails = fast failure ? +1 : 0; sdkcrash = crashed inside the SDK ? +1 : 0;
  fails >= 3 or sdkcrash >= 2: skip (skipped=1)

Skip: no SDK environment variable is set at all (the SDK is not loaded); on the first process of the run one line
`[adot-guard] ADOT SDK skipped for <runtimes>: <reason>` goes to stderr. Inject: exactly as upstream.

## SDK side (each language)

Only when this language's injector agent-path variable is set and the state file (same path rule as the injector)
records this run as injected; standard library only;
every error ignored.
- Healthy timer: started only when the SDK has finished initialising, then writes `<state>.init`; also started if
  initialisation threw (fail-open). After 60 s it writes `<state>.healthy` with the boot id.
- Once the uncaught-exception hook fires for an exception that ends the process (whatever the frame), the timer is
  cancelled: a process that is crashing but slow to exit (exit handlers, flushing) never gets `.healthy` for that run.
- On an uncaught exception that ends the process: take the innermost frame (where it was raised); if its file is
  under the SDK root, write `<state>.crash` and print `[adot-guard] crash inside ADOT SDK: file:line ExcType` to
  stderr. Always hand over to the previous handler. Frames of SDK wrappers in the middle of the stack do not count.

### Per language

| Language | Where the hooks are installed | "Innermost frame is SDK code" means | Crash file line 2 |
|---|---|---|---|
| Python | `sitecustomize.py`: hook before `initialize()`, timer after it returns | `sys.excepthook`; the innermost traceback frame's file is under the SDK directory | `file:line ExcType` |
| Java | `AdotGuardAgent`, the jar's new `Premain-Class`: handler before `AwsAgentBootstrap.premain`, timer after it returns (Java 8 bytecode) | default uncaught exception handler, main thread only and only when no other non-daemon thread is alive (otherwise the JVM keeps running); root cause's frame 0 class name starts with `io.opentelemetry.javaagent.` or `software.amazon.opentelemetry.javaagent.` (agent classes, including everything the agent shades). Class names, not code sources: agent classes live on the bootstrap class path, where the code source is null | `Class.method(File.java:line) ExcType` |
| Node.js | `adot-guard.js`, the injector's `--require` target: monitor before requiring `autoinstrumentation.js`, timer after it returns | `uncaughtExceptionMonitor` (never `uncaughtException`, so the crash itself is unchanged), only when the application has no `uncaughtException` listener; first `at` frame of `err.stack` is a file under the SDK directory | `file:line ErrorName` |
| .NET | startup hook installed as `OpenTelemetry.AutoInstrumentation.StartupHook.dll` (netcoreapp3.1; the original renamed `*.Original.dll` and invoked after): handler before the original `Initialize`, timer after it returns | `AppDomain.UnhandledException` with `IsTerminating`; root cause (innermost `InnerException`) frame 0's method's assembly `Location` under `OTEL_DOTNET_AUTO_HOME/` | `assembly path:Type.Method ExcType` |

Java and .NET follow the cause chain to the exception raised first (reflection and initializers wrap it);
Python and Node use the exception that ended the process. The previous handler always runs afterwards.

## SDK image layout

The images keep today's `/autoinstrumentation` layout, so old-mode injection and older operators work unchanged, and
add two self-referencing symlinks so the same files are also found at the paths upstream expects
(`<prefix>/<glibc|musl>/...`), without patching the injector and without shipping the SDK twice:

    /autoinstrumentation/glibc -> .
    /autoinstrumentation/musl  -> .

The images' `cp` (tools/cp-utility) copies symlinks as symlinks, so the links reach the pod's volume unchanged and
are never followed (no loops, no duplicated files). With the links:
- Python: `<prefix>/glibc` and `<prefix>/musl` are the SDK root; PYTHONPATH is built as in patch 0002. On Alpine the
  glibc native extensions fall back to pure Python, as today.
- .NET: `OTEL_DOTNET_AUTO_HOME=<prefix>/<libc>`, startup hook `<prefix>/<libc>/net/...`, profiler
  `<prefix>/glibc/linux-<arch>/` or `<prefix>/musl/linux-musl-<arch>/` chosen by the detected libc (no runtime
  annotation needed). The minimum .NET version (8) and the deps.json OpenTelemetry stand-down are upstream.
- The guard's `sitecustomize.py` / startup hook / Java Premain wrapper are inert without our `LD_PRELOAD`.

## Logging (patch 0003)

Upstream logs at `Error` by default. Patch 0003 keeps every real error visible (a configured agent path that does
not exist) and makes two normal cases debug-only, because the injector runs in every process of the container:
a runtime with no agent path configured, and an SDK prefix that has no directory for this libc flavour (e.g. a musl
application with a glibc-only SDK; Python, .NET and Ruby). The guard's own lines (`[adot-guard] ADOT SDK
skipped/restored`) are always printed.

## Python PYTHONPATH (patch 0002)

`<sdk>/opentelemetry/instrumentation/auto_instrumentation : <original> : <sdk>`, the same order as the operator uses
today, where `<sdk>` is `<prefix>/<libc>` (a symlink to the SDK root in the ADOT images, see above).

## Architecture

Ship only the `.so` for the node's architecture into the pod. glibc ignores a wrong-architecture or missing
`LD_PRELOAD` entry; musl ignores a missing one but refuses to start the process with a wrong-architecture one
(exit 127). Images are checked at build time (`check-arch.sh`, ELF e_machine 62 = x86_64, 183 = aarch64).

## Test helpers

SDK tests that need "a failure inside SDK code" may add helper modules to a test image only. Release image builds
must fail if any test helper is present under `/autoinstrumentation` (including inside jars).

## Consumers

All four SDK repositories, Python included, consume a pinned `aws-otel-injector-v*` release with checksum
verification; see README.md, "How SDK repositories consume the Injector".

## Init container command

With the injector, the `.so` must be copied with the SDK: `cp -r /autoinstrumentation/. <mount>` (cp-utility picks
the matching `libotelinject_<arch>.so`). Java's current command copies only `/javaagent.jar` (kept, unchanged, for
old mode), so the Java image gains `/autoinstrumentation/{javaagent.jar (with the guard's Premain-Class),injector/}`
and the operator uses the directory copy when the image is the injector-carrying default image.

