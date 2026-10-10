"""Regression coverage for interrupted upgrades and legacy helper omissions."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_selective_restore import REPO, dashboard


class DashboardUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wb-update-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.runtime = self.root / "installed"
        self.runtime.mkdir()
        self.agent = self.runtime / "agent"
        self.agent.mkdir()
        for folder in ("dashboard", "agent"):
            shutil.copytree(REPO / folder, self.project / folder,
                            ignore=shutil.ignore_patterns("__pycache__"))
        (self.runtime / "app.py").write_text("previous app")
        (self.runtime / "sshops.py").write_text("previous SSH module")
        for folder in ("templates", "static"):
            (self.runtime / folder).mkdir()
            (self.runtime / folder / "previous.txt").write_text("previous " + folder)
        (self.agent / "archiveops.py").write_text("previous helper")
        self.preserved = {"dashboard.conf": b"private config", "dashboard.db": b"database",
                          "id_ed25519": b"private key"}
        for name, value in self.preserved.items():
            (self.runtime / name).write_bytes(value)
        self.config = patch.dict(dashboard.CFG, APP_DIR=str(self.runtime), AGENT_DIR=str(self.agent))
        self.config.start()
        self.addCleanup(self.config.stop)

    def assert_old_installation(self):
        self.assertEqual((self.runtime / "app.py").read_text(), "previous app")
        self.assertEqual((self.runtime / "sshops.py").read_text(), "previous SSH module")
        self.assertEqual((self.agent / "archiveops.py").read_text(), "previous helper")
        for folder in ("templates", "static"):
            self.assertEqual((self.runtime / folder / "previous.txt").read_text(), "previous " + folder)
        self.assert_preserved()

    def assert_preserved(self):
        for name, value in self.preserved.items():
            self.assertEqual((self.runtime / name).read_bytes(), value)

    def test_missing_helper_rejects_update_before_changing_files(self):
        (self.project / "agent/archiveops.py").unlink()
        with self.assertRaisesRegex(ValueError, "agent/archiveops.py"):
            dashboard.apply_update_from(str(self.project))
        self.assert_old_installation()

    def test_invalid_python_rejects_update_before_changing_files(self):
        (self.project / "dashboard/app.py").write_text("def invalid(:")
        with self.assertRaises(SyntaxError):
            dashboard.apply_update_from(str(self.project))
        self.assert_old_installation()

    def test_runtime_cannot_be_used_as_its_own_upload_folder(self):
        with patch.dict(dashboard.CFG, APP_DIR=str(self.project / "dashboard")), \
             self.assertRaisesRegex(ValueError, "separate"):
            dashboard.apply_update_from(str(self.project))
        self.assertTrue((self.project / "dashboard/templates/base.html").is_file())

    def test_dependency_failure_keeps_running_files(self):
        pip = self.runtime / "venv/bin/pip"
        pip.parent.mkdir(parents=True)
        pip.touch()
        with patch.object(dashboard.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "pip")), \
             self.assertRaises(subprocess.CalledProcessError):
            dashboard.apply_update_from(str(self.project))
        self.assert_old_installation()

    def test_publish_failure_rolls_back_modules_assets_and_helper(self):
        replace = os.replace
        failed = False

        def fail_app_once(source, destination):
            nonlocal failed
            if not failed and Path(destination) == self.runtime / "app.py" and ".wbupdate-new-" in str(source):
                failed = True
                raise OSError("simulated interrupted publish")
            return replace(source, destination)

        with patch.object(dashboard.os, "replace", side_effect=fail_app_once), \
             self.assertRaisesRegex(OSError, "interrupted"):
            dashboard.apply_update_from(str(self.project))
        self.assertTrue(failed)
        self.assert_old_installation()
        self.assertEqual(list(self.runtime.rglob(".wbupdate-*")), [])

    def test_complete_update_preserves_config_database_and_keys(self):
        dashboard.apply_update_from(str(self.project))
        for src, dst in [(self.project / "dashboard/app.py", self.runtime / "app.py"),
                         (self.project / "dashboard/static/dashboard.js", self.runtime / "static/dashboard.js"),
                         (self.project / "agent/archiveops.py", self.agent / "archiveops.py")]:
            self.assertEqual(src.read_bytes(), dst.read_bytes())
        self.assert_preserved()
        self.assertEqual(list(self.runtime.rglob(".wbupdate-*")), [])

    def test_legacy_update_without_helper_keeps_dashboard_reachable(self):
        runtime = self.root / "legacy/dashboard"
        runtime.mkdir(parents=True)
        agent = runtime / "agent"
        agent.mkdir()
        for name in ("app.py", "sshops.py"):
            shutil.copy2(REPO / "dashboard" / name, runtime / name)
        shutil.copytree(REPO / "dashboard/templates", runtime / "templates")
        config = self.root / "legacy.conf"
        config.write_text("DB_PATH=%s\nAGENT_DIR=%s\n" % (self.root / "legacy.db", agent))
        code = """import sys
sys.path.insert(0, %r)
import app
assert app.archiveops is None
client = app.app.test_client()
assert client.get('/login').status_code == 200
with client.session_transaction() as session:
    session['auth'] = True
response = client.get('/')
assert response.status_code == 200
assert b'Archive helper is missing' in response.data
assert client.get('/settings').status_code == 200
print('Dashboard stays reachable without the archive helper')
""" % str(runtime)
        environment = dict(os.environ, WBD_CONFIG=str(config), WBD_NO_MONITOR="1")
        result = subprocess.run([sys.executable, "-c", code], env=environment,
                                cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
