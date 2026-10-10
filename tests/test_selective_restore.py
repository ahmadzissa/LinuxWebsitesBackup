"""Run with: python -m unittest discover -s tests -v."""
import contextlib
import html
import io
import json
import os
import re
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from werkzeug.datastructures import MultiDict

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agent"))
sys.path.insert(0, str(REPO / "dashboard"))
import archiveops
TEST_STATE = tempfile.TemporaryDirectory(prefix="wb-tests-")
TEST_CONFIG = Path(TEST_STATE.name) / "dashboard.conf"
TEST_CONFIG.write_text("DB_PATH=%s\nAGENT_DIR=%s\n" % (Path(TEST_STATE.name) / "dashboard.db", REPO / "agent"), encoding="utf-8")
with patch.dict(os.environ, WBD_CONFIG=str(TEST_CONFIG), WBD_NO_MONITOR="1"):
    import app as dashboard


def tearDownModule():
    TEST_STATE.cleanup()


def make_archive(path, files=None, links=None):
    files = files or {"public_html/index.php": b"backup index",
                      "public_html/wp-content/themes/store/functions.php": b"backup functions",
                      "public_html/wp-content/themes/store/style.css": b"backup css",
                      "public_html/uploads/my photo.jpg": b"backup photo"}
    with tarfile.open(path, "w:gz") as archive:
        root = tarfile.TarInfo("public_html")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        for name, contents in files.items():
            member = tarfile.TarInfo(name)
            member.size, member.mode, member.mtime = len(contents), 0o640, 1700000000
            archive.addfile(member, io.BytesIO(contents))
        for name, target in (links or {}).items():
            member = tarfile.TarInfo(name)
            member.type, member.linkname = tarfile.SYMTYPE, target
            archive.addfile(member)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name) / "live"
        self.root = self.parent / "public_html"
        self.root.mkdir(parents=True)
        self.archive = Path(self.tmp.name) / "example.com.tar.gz"
        make_archive(self.archive)

    def restore(self, selection):
        with contextlib.redirect_stdout(io.StringIO()):
            archiveops.restore(self.archive, str(self.parent), selection)

    def test_domain_names_for_panels_and_system_paths(self):
        for root, expected in [("/home/user/web/example.com/public_html", "example.com"),
                               ("/home/store/htdocs/Admin.Example.com", "admin.example.com"),
                               ("/var/www/example.com/", "example.com"),
                               ("/usr/share/phpmyadmin/", "usr_share_phpmyadmin")]:
            self.assertEqual(archiveops.archive_name(root), expected)

    def test_single_file_replaces_only_that_file(self):
        (self.root / "index.php").write_text("live index")
        (self.root / "keep.php").write_text("live extra")
        self.restore(["public_html/index.php"])
        self.assertEqual((self.root / "index.php").read_text(), "backup index")
        self.assertEqual((self.root / "keep.php").read_text(), "live extra")
        self.assertFalse((self.root / "wp-content").exists())
        self.assertAlmostEqual((self.root / "index.php").stat().st_mtime, 1700000000, delta=1)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE((self.root / "index.php").stat().st_mode), 0o640)

    def test_folder_and_file_from_another_folder(self):
        theme = self.root / "wp-content/themes/store"
        theme.mkdir(parents=True)
        (theme / "functions.php").write_text("live functions")
        (theme / "extra.php").write_text("keep extra")
        (self.root / "index.php").write_text("keep index")
        self.restore(["public_html/wp-content/themes/store", "public_html/uploads/my photo.jpg"])
        self.assertEqual((theme / "functions.php").read_text(), "backup functions")
        self.assertEqual((theme / "style.css").read_text(), "backup css")
        self.assertEqual((theme / "extra.php").read_text(), "keep extra")
        self.assertEqual((self.root / "index.php").read_text(), "keep index")
        self.assertEqual((self.root / "uploads/my photo.jpg").read_bytes(), b"backup photo")

    def test_missing_selection_leaves_live_files_untouched(self):
        (self.root / "index.php").write_text("live")
        with self.assertRaises(ValueError):
            self.restore(["public_html/index.php", "public_html/missing.php"])
        self.assertEqual((self.root / "index.php").read_text(), "live")

    def test_unsafe_archive_member_leaves_live_files_untouched(self):
        make_archive(self.archive, {"public_html/index.php": b"backup", "public_html/../../escape": b"unsafe"})
        (self.root / "index.php").write_text("live")
        with self.assertRaises(ValueError):
            self.restore(["public_html"])
        self.assertEqual((self.root / "index.php").read_text(), "live")

    def test_invalid_selection_paths(self):
        for path in ["/public_html/index.php", "public_html/../escape", "public_html//index.php", "public_html\\index.php", "public_html/a\n.php"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.restore([path])

    def test_links_are_skipped_in_selected_folders(self):
        make_archive(self.archive, links={"public_html/outside": "/etc/passwd"})
        self.restore(["public_html"])
        self.assertFalse((self.root / "outside").exists())
        with self.assertRaises(ValueError):
            self.restore(["public_html/outside"])

    def test_file_type_conflict_prevents_all_replacements(self):
        (self.root / "index.php").mkdir()
        with self.assertRaises(ValueError):
            self.restore(["public_html"])
        self.assertFalse((self.root / "wp-content").exists())

    def test_symbolic_link_destination_is_rejected(self):
        original_lstat = Path.lstat
        target = self.root / "index.php"
        def lstat(path):
            if path == target:
                return Mock(st_mode=stat.S_IFLNK | 0o777)
            return original_lstat(path)
        with patch.object(Path, "lstat", lstat), self.assertRaisesRegex(ValueError, "symbolic link"):
            self.restore(["public_html/index.php"])
        self.assertFalse(target.exists())

    def test_multiple_roots_and_link_parents_rejected(self):
        for files, links in [({"other/file": b"x"}, None),
                             ({"public_html/link/child": b"x"}, {"public_html/link": "/tmp"})]:
            make_archive(self.archive, files, links)
            with self.archive.open("rb") as stream, self.assertRaises(ValueError):
                archiveops.read_index(stream)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        dashboard.app.config.update(TESTING=True, SECRET_KEY="test-only")
        dashboard.ARCHIVE_CACHE.clear()
        self.row = {"id": 2, "name": "Store server", "host": "192.0.2.2", "port": 22}
        self.parent = "/home/store/web/example.com"
        self.arch = "example.com.tar.gz"
        self.snap = "2026-10-10_0630"
        self.base = "/servers/2/snapshots/" + self.snap
        self.index = archiveops.complete_index([
            {"path": "public_html/index.php", "type": "file", "size": 12, "modified": 0},
            {"path": "public_html/theme/style.css", "type": "file", "size": 5, "modified": 0}])
        self.data = {"index": self.index, "parent": self.parent,
                     "site": dashboard.classify_site(self.arch, self.parent, self.parent + "/public_html")}
        self.client = dashboard.app.test_client()
        with self.client.session_transaction() as session:
            session.update(auth=True, restore_csrf="test-csrf")
        self.addCleanup(patch.stopall)
        patch.object(dashboard, "server_row", return_value=self.row).start()
        self.archive_data = patch.object(dashboard, "archive_data", return_value=self.data).start()
        self.jobs = patch.object(dashboard, "start_job", return_value=123).start()

    def post_selection(self, paths, **kwargs):
        return self.client.post(self.base + "/restore-selected", data=MultiDict(
            [("csrf", "test-csrf"), ("archive", self.arch)] + [("paths", p) for p in paths]), **kwargs)

    def test_friendly_domain_labels_for_legacy_and_new_archives(self):
        for arch, parent, root in [("home_store_web_example.com_public_html.tar.gz", self.parent, ""),
                                  (self.arch, self.parent, self.parent + "/public_html"),
                                  ("home_store_web_example.com_public_html.tar.gz", "", "")]:
            site = dashboard.classify_site(arch, parent, root)
            self.assertEqual(site["label"], "example.com")
        cloud = dashboard.classify_site(self.arch, "/home/store/htdocs", "/home/store/htdocs/example.com")
        self.assertEqual(cloud["path"], "/home/store/htdocs/example.com")

    def test_browser_and_index_are_authenticated(self):
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get(self.base + "/file-index?archive=" + self.arch).status_code, 302)
        self.assertEqual(self.client.get(self.base + "/files/" + self.arch).status_code, 302)

    def test_file_browser_renders_and_index_supplies_actual_root(self):
        self.assertEqual(self.client.get(self.base + "/files/" + self.arch).status_code, 200)
        response = self.client.get(self.base + "/file-index?archive=" + self.arch)
        self.assertEqual(response.json["index"]["root"], "public_html")
        self.assertEqual(response.json["site"]["path"], self.parent + "/public_html")

    def test_review_then_confirm_starts_one_job_with_exact_selection(self):
        selection = ["public_html/index.php", "public_html/theme"]
        response = self.post_selection(selection)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"2 file(s)", response.data)
        self.jobs.assert_not_called()
        token = html.unescape(re.search(r'name="restore_token" value="([^"]+)"', response.text).group(1))
        response = self.client.post(self.base + "/restore-selected", data={"csrf": "test-csrf", "restore_token": token})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/jobs/123"))
        self.jobs.assert_called_once()
        self.assertIn("example.com", self.jobs.call_args.args[2])

    def test_tampered_token_and_csrf_cannot_start_jobs(self):
        for form in [{"csrf": "wrong", "archive": self.arch, "paths": "public_html/index.php"},
                     {"csrf": "test-csrf", "restore_token": "tampered"}]:
            self.assertEqual(self.client.post(self.base + "/restore-selected", data=form).status_code, 400)
        self.jobs.assert_not_called()

    def test_missing_paths_and_mapping_cannot_start_jobs(self):
        for paths in [[], ["public_html/missing"], ["public_html/../../etc/passwd"]]:
            self.assertEqual(self.post_selection(paths).status_code, 302)
        self.data["parent"] = ""
        self.assertEqual(self.post_selection(["public_html/index.php"]).status_code, 302)
        self.jobs.assert_not_called()

    def test_archive_parameter_cannot_escape_snapshot(self):
        self.assertEqual(self.client.get(self.base + "/file-index?archive=../../etc/passwd").status_code, 400)
        self.archive_data.assert_not_called()

    def test_legacy_tar_stream_and_new_index_have_identical_entries(self):
        # Restore the real module function for the NAS transport compatibility test.
        patch.stopall()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.tar.gz"
            make_archive(path)
            archive_bytes = path.read_bytes()
            index = archiveops.read_index(io.BytesIO(archive_bytes))
        for indexed in (False, True):
            dashboard.ARCHIVE_CACHE.clear()
            nas, channel = Mock(), Mock()
            channel.makefile.return_value = io.BytesIO(archive_bytes)
            channel.recv.return_value = b""
            channel.recv_exit_status.return_value = 0
            nas.get_transport.return_value.open_session.return_value = channel
            def run(client, cmd, **kwargs):
                if "PATHS.txt" in cmd:
                    return 0, self.arch + "\t" + self.parent + "\n"
                if "ROOTS.txt" in cmd:
                    return 1, ""
                return (0, json.dumps(index)) if indexed else (1, "")
            with patch.object(dashboard, "server_hostname", return_value="server"), \
                 patch.object(dashboard, "get_setting", side_effect=lambda key: "/volume1/WebBackups" if key == "syno_path" else "nas"), \
                 patch.object(dashboard, "nas_connect", return_value=nas), patch.object(dashboard.sshops, "run", side_effect=run):
                result = dashboard.archive_data(self.row, self.snap, self.arch)
                self.assertEqual(result["index"], index)
                self.assertEqual(result["site"]["path"], self.parent + "/public_html")
                self.assertEqual(channel.exec_command.call_count, 0 if indexed else 1)
                nas.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
