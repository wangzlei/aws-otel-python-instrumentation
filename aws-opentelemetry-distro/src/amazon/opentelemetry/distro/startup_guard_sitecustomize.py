# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""SDK side of the ADOT start-up guard for zero-code instrumentation on Kubernetes.

The operator auto-instrumentation image (see the repository Dockerfile) installs this file as
``opentelemetry/instrumentation/auto_instrumentation/sitecustomize.py``, replacing the upstream one: it runs
``initialize()`` exactly like upstream, and additionally reports how the run went to the OTel Injector, which decides
at the next container start whether to load the SDK (contract: ``tools/otel-injector/SPEC.md`` of
aws-otel-python-instrumentation).

The hooks are active only when the Injector set up Python in this process
(``PYTHON_AUTO_INSTRUMENTATION_AGENT_PATH_PREFIX`` and our ``libotelinject.so`` in ``LD_PRELOAD``) and its state file
records the current container run as injected.
Otherwise, including for ``opentelemetry-instrument`` and every non-Kubernetes use, nothing changes. Files written
next to the Injector's state file:

* ``<state>.init``: the boot id and the seconds since node boot, when the SDK finished initialising
* ``<state>.healthy``: the boot id, 60 s after that, unless the run is ending on an uncaught exception
* ``<state>.crash``: the boot id and ``file:line ExcType``, when an uncaught exception raised inside the SDK ends the
  process

This module must only use the standard library and must never raise: it runs before the SDK is imported, and the SDK's
own dependencies may be the problem it reports.
"""
# pylint: disable=broad-exception-caught,import-outside-toplevel,too-many-locals,too-many-statements

HEALTHY_SECONDS = 60.0
CRASH_DESCRIPTION_MAX = 400
INJECTOR_LIBRARY = "libotelinject.so"
AGENT_PATH_VARIABLE = "PYTHON_AUTO_INSTRUMENTATION_AGENT_PATH_PREFIX"


def install_hooks(sdk_root, environ=None, proc="/proc"):
    """Install the crash hook and return a function to call once the SDK has finished initialising, or None when the
    guard is not active for this run. ``sdk_root`` is the directory the SDK was copied to (crashes raised in files under
    it count as SDK crashes)."""
    import os
    import re
    import sys
    import threading

    if environ is None:
        environ = os.environ
    # Only when the Injector set up this language: an SDK loaded the old way must not write records.
    if not environ.get(AGENT_PATH_VARIABLE):
        return None
    state_dir = None
    for entry in re.split(r"[: ]+", environ.get("LD_PRELOAD", "")):
        if entry.startswith("/") and os.path.basename(entry) == INJECTOR_LIBRARY:
            directory = os.path.dirname(entry)
            if os.path.basename(directory) == "injector":
                directory = os.path.dirname(directory)
            if len(directory) > 1:
                state_dir = directory
            break
    if state_dir is None:
        return None

    # <id>: the container name from the kubelet's termination-log bind mount, else FNV-1a 32 of PID 1's command line.
    name = None
    container = re.compile(r"/pods/[0-9a-f-]{36}/containers/([a-z0-9.-]{1,63})/[^/]+$")
    with open(os.path.join(proc, "self", "mountinfo"), encoding="utf-8", errors="replace") as mountinfo:
        for line in mountinfo:
            fields = line.split()
            match = container.search(fields[3]) if len(fields) > 3 else None
            if match:
                name = match.group(1)
                break
    if name is None:
        with open(os.path.join(proc, "1", "cmdline"), "rb") as cmdline_file:
            cmdline = cmdline_file.read()
        digest = 0x811C9DC5
        for byte in bytearray(cmdline):
            digest = ((digest ^ byte) * 0x01000193) & 0xFFFFFFFF
        name = f"{digest:08x}"
    state = os.path.join(state_dir, ".adot-guard-" + name)
    with open(os.path.join(proc, "1", "stat"), encoding="utf-8") as stat_file:
        boot = stat_file.read().rsplit(")", 1)[1].split()[19]  # field 22: PID 1 start time, changes on every restart
    with open(state, encoding="utf-8") as state_file:
        recorded = dict(kv.split("=", 1) for kv in state_file.read().split() if "=" in kv)
    if recorded.get("boot") != boot or recorded.get("skipped") == "1":
        return None  # the Injector did not record this run as injected

    root = os.path.abspath(sdk_root)
    lock = threading.Lock()
    run = {"crashed": False, "timer": None}

    def write(path, data):
        tmp = f"{path}.{os.getpid()}.{int.from_bytes(os.urandom(4), 'big'):08x}"
        try:
            descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o666)
            try:
                os.write(descriptor, data.encode())
            finally:
                os.close(descriptor)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise

    def mark_healthy():
        try:
            with lock:
                if run["crashed"]:
                    return  # this run is ending on an uncaught exception: never healthy
            write(state + ".healthy", boot + "\n")
        except Exception:
            pass

    def initialised():
        try:
            with lock:
                if run["crashed"] or run["timer"] is not None:
                    return
                timer = threading.Timer(HEALTHY_SECONDS, mark_healthy)
                timer.daemon = True
                run["timer"] = timer
            with open(os.path.join(proc, "uptime"), encoding="utf-8") as uptime:
                write(state + ".init", f"{boot}\n{uptime.read().split()[0]}\n")
            timer.start()
        except Exception:
            pass

    previous_hook = sys.excepthook

    def hook(exc_type, value, traceback):
        try:
            with lock:
                run["crashed"] = True
                if run["timer"] is not None:
                    run["timer"].cancel()
            last = traceback
            while last is not None and last.tb_next is not None:
                last = last.tb_next  # innermost frame: where the exception was raised
            if last is not None:
                source = os.path.abspath(last.tb_frame.f_code.co_filename)
                if source.startswith(root + os.sep):
                    description = f"{source}:{last.tb_lineno} {getattr(exc_type, '__name__', '?')}"
                    description = description[-CRASH_DESCRIPTION_MAX:]
                    write(state + ".crash", f"{boot}\n{description}\n")
                    sys.stderr.write(f"[adot-guard] crash inside ADOT SDK: {description}\n")
        except Exception:
            pass
        previous_hook(exc_type, value, traceback)

    sys.excepthook = hook
    return initialised


def run_as_sitecustomize(path):
    """What the installed ``sitecustomize.py`` does: the upstream ``initialize()``, wrapped by the guard hooks.
    ``path`` is the file's own path, ``<sdk>/opentelemetry/instrumentation/auto_instrumentation/sitecustomize.py``."""
    import os

    sdk_root = path
    for _ in range(4):
        sdk_root = os.path.dirname(sdk_root)
    try:
        initialised = install_hooks(sdk_root)
    except Exception:
        initialised = None
    try:
        from opentelemetry.instrumentation.auto_instrumentation import initialize

        initialize()
    finally:
        # The 60 s count starts once the SDK has finished initialising, so a slow SDK start-up cannot make a run that
        # fails right after it look healthy; also when initialisation failed (fail-open).
        if initialised is not None:
            initialised()


if __name__ == "sitecustomize":
    run_as_sitecustomize(__file__)
