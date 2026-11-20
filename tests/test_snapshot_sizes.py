"""Per-website archive sizes and per-database dump sizes from NAS metadata."""
import re
import unittest
from unittest.mock import Mock, patch

from test_selective_restore import dashboard


class SnapshotSizeTests(unittest.TestCase):
    def setUp(self):
        dashboard.app.config.update(TESTING=True, SECRET_KEY="test-only")
        self.client = dashboard.app.test_client()
        with self.client.session_transaction() as session:
            session["auth"] = True
        self.row = {"id": 2, "name": "Store server", "host": "192.0.2.2"}
        self.url = "/servers/2/snapshots/2026-10-10_0630"
        self.nas = Mock()
        self.listing = "\n".join([
            "1048576\tfiles/example.com.tar.gz",
            "2097152\tfiles/home_store_web_shop.example.com_public_html.tar.gz",
            "1024\tfiles/etc.tar.gz",
            "4096\tdatabases/mysql_store.sql.gz",
            "999999\tdatabases/mysql_all_databases.tar",
            "123\tconfigs/nginx.tar.gz"])
        self.members = "\n".join([
            "-rw-r--r-- root/root     8192 2026-10-10 06:30 mysql_store.sql.gz",
            "-rw-r--r-- 0/0       3145728 2026-10-10 06:30:00 mysql_shop.sql.gz",
            "-rw-r--r-- root/root        0 2026-10-10 06:30 mysql_empty.sql.gz",
            "-rw-r--r-- root/root      100 2026-10-10 06:30 notes.txt"])
        self.verbose_ok = True
        for attribute, value in [("server_row", self.row), ("server_hostname", "server"),
                                 ("nas_connect", self.nas), ("get_setting", "/volume1/WebBackups"),
                                 ("snapshot_maps", ({"example.com.tar.gz": "/home/store/htdocs",
                                                     "home_store_web_shop.example.com_public_html.tar.gz":
                                                     "/home/store/web/shop.example.com",
                                                     "etc.tar.gz": "/"}, {}))]:
            mocked = patch.object(dashboard, attribute, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        run = patch.object(dashboard.sshops, "run", side_effect=self.run_nas)
        self.run_mock = run.start()
        self.addCleanup(run.stop)

    def run_nas(self, client, command, **kwargs):
        if command.startswith("cd "):
            return 0, self.listing
        if command.startswith("LC_ALL=C tar -tvf "):
            return (0, self.members) if self.verbose_ok else (1, "")
        if command.startswith("tar -tf "):
            return 0, "mysql_name_only.sql.gz\n"
        raise AssertionError("Unexpected NAS command: " + command)

    def rows(self, response):
        self.assertEqual(response.status_code, 200)
        return re.findall(r"<tr>(.*?)</tr>", response.get_data(as_text=True), re.S)

    def row_with(self, rows, text):
        return next(row for row in rows if text in row)

    def test_each_website_and_folder_has_its_own_archive_size(self):
        rows = self.rows(self.client.get(self.url))
        self.assertIn("1.0 MB", self.row_with(rows, "<strong>example.com</strong>"))
        self.assertIn("2.0 MB", self.row_with(rows, "<strong>shop.example.com</strong>"))
        self.assertIn("1.0 KB", self.row_with(rows, "etc.tar.gz"))
        self.nas.close.assert_called_once()
        # Listing sizes never opens or decompresses the website archives.
        self.assertEqual(self.run_mock.call_count, 2)

    def test_individual_bundle_sizes_and_loose_dump_precedence(self):
        rows = self.rows(self.client.get(self.url))
        store = self.row_with(rows, 'class="mono">store</td>')
        self.assertIn("4.0 KB", store)
        self.assertNotIn("8.0 KB", store)
        self.assertIn("3.0 MB", self.row_with(rows, 'class="mono">shop</td>'))
        self.assertIn("0 B", self.row_with(rows, 'class="mono">empty</td>'))
        self.assertNotIn('class="mono">notes</td>', "".join(rows))
        self.assertEqual(len([row for row in rows if 'class="mono">store</td>' in row]), 1)

    def test_unavailable_or_invalid_member_sizes_remain_unknown(self):
        self.members = "-rw-r--r-- root/root unknown 2026-10-10 06:30 mysql_unknown.sql.gz"
        rows = self.rows(self.client.get(self.url))
        self.assertIn("—", self.row_with(rows, 'class="mono">unknown</td>'))
        self.verbose_ok = False
        rows = self.rows(self.client.get(self.url))
        self.assertIn("—", self.row_with(rows, 'class="mono">name_only</td>'))
        self.assertIn("4.0 KB", self.row_with(rows, 'class="mono">store</td>'))

    def test_snapshot_sizes_are_authenticated_and_nas_closes_on_failure(self):
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.run_mock.assert_not_called()
        with self.client.session_transaction() as session:
            session["auth"] = True
        self.run_mock.side_effect = RuntimeError("NAS unavailable")
        response = self.client.get(self.url)
        self.assertIn(b"Could not read the snapshot", response.data)
        self.nas.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
