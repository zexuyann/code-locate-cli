from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest

from code_locate.cli import main


class CliTest(unittest.TestCase):
    def test_rejects_negative_top(self) -> None:
        with self.assertRaises(SystemExit):
            main(["query", "needle", "--top", "-1"])

    def test_rejects_expand_depth_above_limit(self) -> None:
        with self.assertRaises(SystemExit):
            main(["expand", "src/app.py", "--depth", "4"])

    def test_rejects_top_above_limit(self) -> None:
        with self.assertRaises(SystemExit):
            main(["query", "needle", "--top", "101"])

    def test_runtime_errors_are_json_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main(["query", "--repo", tmp, "--json"])

        self.assertEqual(code, 2)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["error"]["type"], "ValueError")

    def test_argument_errors_are_json_when_requested(self) -> None:
        stderr = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(stderr):
            main(["query", "needle", "--top", "-1", "--json"])

        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["error"]["type"], "ArgumentError")
        self.assertIn("top must be >= 1", payload["error"]["message"])


if __name__ == "__main__":
    unittest.main()
