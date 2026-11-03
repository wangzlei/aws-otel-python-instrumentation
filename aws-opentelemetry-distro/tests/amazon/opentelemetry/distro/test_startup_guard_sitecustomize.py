# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import os
import sys
import tempfile
import time
import types
from unittest import TestCase
from unittest.mock import MagicMock, patch

from amazon.opentelemetry.distro import startup_guard_sitecustomize as guard

BOOT = "4242"
POD_UID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"


class TestStartupGuardSitecustomize(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        base = self.tmp.name
        self.proc = os.path.join(base, "proc")
        os.makedirs(os.path.join(self.proc, "self"))
        os.makedirs(os.path.join(self.proc, "1"))
        self.volume = os.path.join(base, "otel-auto-instrumentation-python")
        os.makedirs(os.path.join(self.volume, "injector"))
        self.sdk_file = os.path.join(self.volume, "opentelemetry", "sdkmodule.py")
        os.makedirs(os.path.dirname(self.sdk_file))
        self.write_proc(container="app")
        self.state = os.path.join(self.volume, ".adot-guard-app")
        self.write_state(f"boot={BOOT} fails=0 pod=p restored=0 sdkcrash=0 skipfails=0 skipped=0")
        self.environ = {
            guard.AGENT_PATH_VARIABLE: self.volume,
            "LD_PRELOAD": os.path.join(self.volume, "injector", guard.INJECTOR_LIBRARY),
        }
        self.saved_hook = sys.excepthook
        self.previous = MagicMock()
        sys.excepthook = self.previous

    def tearDown(self):
        sys.excepthook = self.saved_hook
        self.tmp.cleanup()

    def write_proc(self, container=None, cmdline=b"python\x00/app.py\x00"):
        mount = f"/var/lib/kubelet/pods/{POD_UID}/containers/{container}/6732171a" if container else "/etc/hosts"
        with open(os.path.join(self.proc, "self", "mountinfo"), "w", encoding="utf-8") as handle:
            handle.write(f"1993 1984 259:1 {mount} /dev/termination-log rw,noatime - xfs /dev/nvme0n1p1 rw\n")
        with open(os.path.join(self.proc, "1", "cmdline"), "wb") as handle:
            handle.write(cmdline)
        fields = ["S"] + ["0"] * 18 + [BOOT] + ["0"] * 10
        with open(os.path.join(self.proc, "1", "stat"), "w", encoding="utf-8") as handle:
            handle.write("1 (python app) " + " ".join(fields) + "\n")
        with open(os.path.join(self.proc, "uptime"), "w", encoding="utf-8") as handle:
            handle.write("12345.67 99999.00\n")

    def write_state(self, content):
        with open(self.state, "w", encoding="utf-8") as handle:
            handle.write(content)

    def read(self, suffix):
        with open(self.state + suffix, encoding="utf-8") as handle:
            return handle.read()

    def install(self):
        return guard.install_hooks(self.volume, environ=self.environ, proc=self.proc)

    def raise_in_sdk(self):
        code = compile("raise ValueError('boom')", self.sdk_file, "exec")
        try:
            exec(code, {})  # pylint: disable=exec-used
        except ValueError:
            return sys.exc_info()
        raise AssertionError("not raised")

    def test_inactive_without_agent_path(self):
        del self.environ[guard.AGENT_PATH_VARIABLE]
        self.assertIsNone(self.install())
        self.assertIs(sys.excepthook, self.previous)

    def test_inactive_without_injector_in_ld_preload(self):
        self.environ["LD_PRELOAD"] = "/usr/lib/libfoo.so"
        self.assertIsNone(self.install())
        self.assertIs(sys.excepthook, self.previous)

    def test_inactive_when_run_not_recorded_as_injected(self):
        for content in ("boot=1 fails=0 skipped=0", f"boot={BOOT} fails=3 skipped=1"):
            self.write_state(content)
            self.assertIsNone(self.install())
        self.assertIs(sys.excepthook, self.previous)

    def test_missing_state_file_raises_so_caller_fails_open(self):
        os.unlink(self.state)
        with self.assertRaises(OSError):
            self.install()

    def test_init_then_healthy(self):
        with patch.object(guard, "HEALTHY_SECONDS", 0.05):
            initialised = self.install()
            self.assertIsNotNone(initialised)
            initialised()
            self.assertEqual(f"{BOOT}\n12345.67\n", self.read(".init"))
            deadline = time.time() + 5
            while not os.path.exists(self.state + ".healthy") and time.time() < deadline:
                time.sleep(0.01)
        self.assertEqual(BOOT + "\n", self.read(".healthy"))
        self.assertEqual(
            [".adot-guard-app", ".adot-guard-app.healthy", ".adot-guard-app.init", "injector", "opentelemetry"],
            sorted(os.listdir(self.volume)),
            "no temporary files left",
        )

    def test_crash_inside_sdk_is_recorded_and_cancels_healthy(self):
        with patch.object(guard, "HEALTHY_SECONDS", 0.2):
            initialised = self.install()
            initialised()
            exc_info = self.raise_in_sdk()
            sys.excepthook(*exc_info)
            time.sleep(0.4)
        self.previous.assert_called_once_with(*exc_info)
        line1, line2, _ = self.read(".crash").split("\n")
        self.assertEqual(BOOT, line1)
        self.assertEqual(f"{self.sdk_file}:1 ValueError", line2)
        self.assertFalse(os.path.exists(self.state + ".healthy"))

    def test_crash_outside_sdk_is_not_recorded(self):
        initialised = self.install()
        initialised()
        exc_info = None
        try:
            raise KeyError("app bug")
        except KeyError:
            exc_info = sys.exc_info()
        sys.excepthook(*exc_info)
        self.previous.assert_called_once_with(*exc_info)
        self.assertFalse(os.path.exists(self.state + ".crash"))

    def test_crash_description_is_bounded(self):
        self.sdk_file = os.path.join(self.volume, "opentelemetry", "x" * 200, "y" * 200, "z.py")
        os.makedirs(os.path.dirname(self.sdk_file))
        self.install()
        sys.excepthook(*self.raise_in_sdk())
        self.assertLessEqual(len(self.read(".crash").split("\n")[1]), guard.CRASH_DESCRIPTION_MAX)

    def test_container_name_falls_back_to_cmdline_hash(self):
        self.write_proc(container=None, cmdline=b"python\x00/app.py\x00")
        self.state = os.path.join(self.volume, ".adot-guard-be13029a")
        self.write_state(f"boot={BOOT} fails=0 skipped=0")
        initialised = self.install()
        self.assertIsNotNone(initialised)
        initialised()
        self.assertTrue(os.path.exists(self.state + ".init"))

    def test_run_as_sitecustomize_calls_upstream_initialize(self):
        calls = []
        fake = types.ModuleType("opentelemetry.instrumentation.auto_instrumentation")
        fake.initialize = lambda: calls.append("initialize")
        path = os.path.join(self.volume, "opentelemetry", "instrumentation", "auto_instrumentation", "sitecustomize.py")
        with patch.dict(sys.modules, {"opentelemetry.instrumentation.auto_instrumentation": fake}), patch.object(
            guard, "install_hooks", return_value=lambda: calls.append("initialised")
        ) as install:
            guard.run_as_sitecustomize(path)
        install.assert_called_once_with(os.path.dirname(self.volume) + os.sep + os.path.basename(self.volume))
        self.assertEqual(["initialize", "initialised"], calls)

    def test_run_as_sitecustomize_fails_open(self):
        calls = []
        fake = types.ModuleType("opentelemetry.instrumentation.auto_instrumentation")
        fake.initialize = lambda: calls.append("initialize")
        with patch.dict(sys.modules, {"opentelemetry.instrumentation.auto_instrumentation": fake}), patch.object(
            guard, "install_hooks", side_effect=OSError("no proc")
        ):
            guard.run_as_sitecustomize("/a/b/c/d/sitecustomize.py")
        self.assertEqual(["initialize"], calls)
