from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from code_locate.symbols import find_enclosing_symbol


class SymbolsTest(unittest.TestCase):
    def test_finds_typescript_function_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.ts"
            path.write_text(
                textwrap.dedent(
                    """
                    export function saveConfig(value: Settings) {
                      localStorage.setItem("settings", JSON.stringify(value));
                    }
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            symbol = find_enclosing_symbol(path, 2)

        self.assertIsNotNone(symbol)
        self.assertEqual(symbol.name, "saveConfig")
        self.assertIn(symbol.kind, {"function", "symbol"})

    def test_finds_python_multiline_function_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "search.py"
            path.write_text(
                textwrap.dedent(
                    """
                    def collect_matches(
                        plan,
                        repo,
                    ):
                        return plan, repo

                    def other():
                        return None
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            symbol = find_enclosing_symbol(path, 2)

        self.assertIsNotNone(symbol)
        self.assertEqual(symbol.name, "collect_matches")
        self.assertEqual(symbol.start_line, 1)
        self.assertEqual(symbol.end_line, 5)


if __name__ == "__main__":
    unittest.main()
