from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from code_locate.expand import expand_location


class ExpandTest(unittest.TestCase):
    def test_expands_symbol_dependencies_and_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            src = repo / "src"
            tests = repo / "tests"
            src.mkdir()
            tests.mkdir()
            (src / "db.py").write_text(
                textwrap.dedent(
                    """
                    def save_record(value):
                        return value
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            (src / "store.py").write_text(
                textwrap.dedent(
                    """
                    from .db import save_record
                    import json

                    def save_config(value):
                        payload = normalize(value)
                        save_record(payload)
                        return json.dumps(payload)

                    def normalize(value):
                        return value
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            (tests / "test_store.py").write_text(
                textwrap.dedent(
                    """
                    from src.store import save_config

                    def test_save_config():
                        save_config({"theme": "dark"})
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            payload = expand_location(repo, "src/store.py", 5, top=10)

        self.assertEqual(payload["target"]["scope"], "symbol")
        self.assertEqual(payload["target"]["symbol"]["name"], "save_config")
        self.assertIn("src/db.py", {item["path"] for item in payload["dependencies"]})
        self.assertIn("tests/test_store.py", {item["path"] for item in payload["dependents"]})
        self.assertIn("normalize", {item["name"] for item in payload["local_callees"]})
        self.assertIn("save_record", {item["name"] for item in payload["imported_callees"]})
        self.assertIn("tests/test_store.py", {item["path"] for item in payload["incoming_references"]})
        self.assertGreaterEqual(len(payload["graph"]["edges"]), 4)
        self.assertIn("analysis", payload)
        self.assertIsInstance(payload["suggested_next_steps"][0]["argv"], list)

    def test_expand_does_not_treat_super_init_as_local_recursive_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            path = repo / "models.py"
            path.write_text(
                textwrap.dedent(
                    """
                    class Child(Base):
                        def __init__(self):
                            super().__init__()
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            payload = expand_location(repo, "models.py", 2, top=10)

        self.assertNotIn("__init__", {item["name"] for item in payload["local_callees"]})
        self.assertEqual(payload["incoming_references"], [])

    def test_tree_sitter_resolves_python_super_call_to_imported_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            src = repo / "src"
            src.mkdir()
            (src / "base.py").write_text(
                textwrap.dedent(
                    """
                    class Base:
                        def __init__(self):
                            pass
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            (src / "models.py").write_text(
                textwrap.dedent(
                    """
                    from .base import Base

                    class Child(Base):
                        def __init__(self):
                            super().__init__()
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            payload = expand_location(repo, "src/models.py", 4, top=10)

        super_edges = [
            item for item in payload["imported_callees"]
            if item.get("relation") == "super_call"
        ]
        self.assertTrue(super_edges)
        self.assertEqual(super_edges[0]["resolved_path"], "src/base.py")
        self.assertEqual(super_edges[0]["resolved_symbol"]["name"], "__init__")

    def test_dependents_avoid_short_substring_false_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "rst.py").write_text("class RST:\n    pass\n", encoding="utf-8")
            (repo / "other.py").write_text("from somewhere import first\n", encoding="utf-8")

            payload = expand_location(repo, "rst.py", 1, top=10)

        self.assertEqual(payload["dependents"], [])

    def test_tree_sitter_extracts_java_go_and_c_syntax_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "Demo.java").write_text(
                "import java.util.List;\nclass Demo { void run() { helper(); } void helper() {} }\n",
                encoding="utf-8",
            )
            (repo / "main.go").write_text(
                'package main\nimport "fmt"\nfunc run() { fmt.Println("x") }\n',
                encoding="utf-8",
            )
            (repo / "main.c").write_text(
                '#include "foo.h"\nvoid run() { helper(); }\n',
                encoding="utf-8",
            )

            java_payload = expand_location(repo, "Demo.java", top=10)
            go_payload = expand_location(repo, "main.go", top=10)
            c_payload = expand_location(repo, "main.c", top=10)

        self.assertIn("java.util.List", {item["module"] for item in java_payload["imports"]})
        self.assertIn("helper", {item["base_name"] for item in java_payload["outgoing_calls"]})
        self.assertIn("fmt", {item["module"] for item in go_payload["imports"]})
        self.assertIn("Println", {item["base_name"] for item in go_payload["outgoing_calls"]})
        self.assertIn("foo.h", {item["module"] for item in c_payload["imports"]})
        self.assertIn("helper", {item["base_name"] for item in c_payload["outgoing_calls"]})


if __name__ == "__main__":
    unittest.main()
