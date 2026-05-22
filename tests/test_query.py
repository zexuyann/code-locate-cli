from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from code_locate.models import Candidate, Symbol
from code_locate.ranking import rank_matches
from code_locate.search import collect_matches
from code_locate.search_plan import SearchPlan


class QueryTest(unittest.TestCase):
    def test_collect_and_rank_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            src = repo / "src"
            src.mkdir()
            (src / "settings.ts").write_text(
                textwrap.dedent(
                    """
                    export function saveConfig(value: Settings) {
                      localStorage.setItem("settings", JSON.stringify(value));
                    }

                    export function loadConfig() {
                      return JSON.parse(localStorage.getItem("settings") || "{}");
                    }
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            plan = SearchPlan.from_dict(
                {
                    "issue": "settings disappear after refresh",
                    "identifiers": ["saveConfig"],
                    "storage_terms": ["localStorage"],
                    "concept_terms": ["settings"],
                }
            )

            matches = collect_matches(plan, repo)
            results = rank_matches(matches, repo, top=5)

        self.assertGreaterEqual(len(matches), 3)
        self.assertTrue(results)
        self.assertEqual(results[0].path, "src/settings.ts")
        self.assertIn("saveConfig", results[0].matched_terms)
        self.assertIsInstance(results[0].to_dict(1)["suggested_next_steps"][0]["argv"], list)

    def test_identifier_terms_do_not_match_substrings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "rst.py").write_text("class RST:\n    pass\n", encoding="utf-8")
            (repo / "noise.py").write_text("first = 'not the symbol'\n", encoding="utf-8")
            plan = SearchPlan.from_dict({"issue": "rst bug", "identifiers": ["RST"]})

            matches = collect_matches(plan, repo)

        self.assertEqual({match.path for match in matches}, {"rst.py"})

    def test_identifier_like_exact_phrases_do_not_match_substrings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "rst.py").write_text("format = 'RST'\n", encoding="utf-8")
            (repo / "noise.py").write_text("first = 'not the format'\n", encoding="utf-8")
            plan = SearchPlan.from_dict({"issue": "rst bug", "exact_phrases": ["RST"]})

            matches = collect_matches(plan, repo)

        self.assertEqual({match.path for match in matches}, {"rst.py"})

    def test_default_excludes_skip_virtualenv_like_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / ".venv").mkdir()
            (repo / "src" / "app.py").write_text("needle = 1\n", encoding="utf-8")
            (repo / ".venv" / "site.py").write_text("needle = 2\n", encoding="utf-8")
            plan = SearchPlan.from_dict({"issue": "needle", "concept_terms": ["needle"]})

            matches = collect_matches(plan, repo)

        self.assertEqual({match.path for match in matches}, {"src/app.py"})

    def test_default_excludes_skip_hidden_env_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / ".env").write_text("needle=secret\n", encoding="utf-8")
            (repo / "src" / "app.py").write_text("needle = 1\n", encoding="utf-8")
            plan = SearchPlan.from_dict({"issue": "needle", "concept_terms": ["needle"]})

            matches = collect_matches(plan, repo)

        self.assertEqual({match.path for match in matches}, {"src/app.py"})

    def test_python_fallback_skips_symlinks_outside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            repo = Path(tmp)
            src = repo / "src"
            src.mkdir()
            (src / "app.py").write_text("needle = 1\n", encoding="utf-8")
            outside_file = Path(outside) / "secret.py"
            outside_file.write_text("needle = 2\n", encoding="utf-8")
            try:
                (repo / "linked_secret.py").symlink_to(outside_file)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            plan = SearchPlan.from_dict({"issue": "needle", "concept_terms": ["needle"]})

            with patch("code_locate.search.shutil.which", return_value=None):
                matches = collect_matches(plan, repo)

        self.assertEqual({match.path for match in matches}, {"src/app.py"})

    def test_suggested_steps_are_structured_argv(self) -> None:
        candidate = Candidate(
            path="src/name;rm.py",
            start_line=1,
            end_line=1,
            symbol=Symbol(name="save;config", kind="function", start_line=1, end_line=1),
        )

        payload = candidate.to_dict(1)
        first_step = payload["suggested_next_steps"][0]

        self.assertEqual(first_step["argv"][2], "src/name;rm.py:1")
        self.assertIn("display", first_step)


if __name__ == "__main__":
    unittest.main()
