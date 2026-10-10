"""Live job logs retain authentication, text, and final status across polls."""
import gc
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_selective_restore import dashboard


class JobOutputTests(unittest.TestCase):
    def setUp(self):
        state = tempfile.TemporaryDirectory(prefix="wb-job-tests-")
        self.addCleanup(state.cleanup)
        config = patch.dict(dashboard.CFG, DB_PATH=str(Path(state.name) / "jobs.db"))
        config.start()
        self.addCleanup(config.stop)
        # SQLite connections can wait for cyclic GC; release Windows file
        # handles before removing this test's disposable database directory.
        self.addCleanup(gc.collect)
        dashboard.init_db()
        dashboard.app.config.update(TESTING=True, SECRET_KEY="test-only")
        self.client = dashboard.app.test_client()
        with self.client.session_transaction() as session:
            session["auth"] = True
        self.job_id = dashboard.create_job(None, "Test workspace", "log test")
        self.append = dashboard.job_appender(self.job_id)
        self.url = "/jobs/%s/output" % self.job_id

    def test_output_requires_login_and_unknown_jobs_return_404(self):
        self.append("private job output")
        with self.client.session_transaction() as session:
            session.clear()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(b"private job output", response.data)
        with self.client.session_transaction() as session:
            session["auth"] = True
        self.assertEqual(self.client.get("/jobs/9999/output").status_code, 404)

    def test_incremental_unicode_and_partial_lines_are_not_duplicated(self):
        original = "Starting backup \U0001f680 — مرحبا\nProgress: "
        self.append(original, raw=True)
        response = self.client.get(self.url)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        data = response.get_json()
        self.assertEqual(data["output"], original)
        self.assertEqual(data["offset"], len(original))
        self.append("50%\x00\n", raw=True)
        data = self.client.get(self.url, query_string={"offset": data["offset"]}).get_json()
        self.assertEqual(data["output"], "50%\x00\n")
        self.assertFalse(data["reset"])
        repeated = self.client.get(self.url, query_string={"offset": data["offset"]}).get_json()
        self.assertEqual(repeated["output"], "")
        self.assertEqual(repeated["status"], "running")

    def test_final_response_contains_last_output_and_completion(self):
        for ok, expected in [(True, "ok"), (False, "fail")]:
            with self.subTest(status=expected):
                self.append("Final line")
                dashboard.finish_job(self.job_id, ok)
                data = self.client.get(self.url).get_json()
                self.assertTrue(data["output"].endswith("Final line\n"))
                self.assertEqual(data["status"], expected)
                self.assertTrue(data["finished"])
                page = self.client.get("/jobs/%s" % self.job_id).get_data(as_text=True)
                self.assertNotIn("job-output.js", page)
                self.assertNotIn('http-equiv="refresh"', page)

    def test_stale_offset_resets_log_and_invalid_offsets_are_rejected(self):
        self.append("Current log")
        data = self.client.get(self.url, query_string={"offset": 100000}).get_json()
        self.assertTrue(data["reset"])
        self.assertEqual(data["output"], "Current log\n")
        for offset in ["-1", "bad", "1.5", ""]:
            with self.subTest(offset=offset):
                self.assertEqual(self.client.get(self.url, query_string={"offset": offset}).status_code, 400)

    def test_running_page_loads_live_script_and_escapes_log_text(self):
        self.append("<script>alert('log text')</script> \U0001f680", raw=True)
        page = self.client.get("/jobs/%s" % self.job_id).get_data(as_text=True)
        self.assertIn("job-output.js", page)
        self.assertIn('data-output-offset="%s"' % len("<script>alert('log text')</script> \U0001f680"), page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>alert", page)
        self.assertNotIn('http-equiv="refresh"', page)


if __name__ == "__main__":
    unittest.main()
