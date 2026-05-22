from __future__ import annotations

import unittest

from code_locate.search_plan import SearchPlan


class SearchPlanTest(unittest.TestCase):
    def test_from_dict_dedupes_terms(self) -> None:
        plan = SearchPlan.from_dict(
            {
                "issue": "settings lost",
                "identifiers": ["saveConfig", "saveConfig", "loadConfig"],
                "concept_terms": ["settings", "Settings"],
                "exclude_globs": ["tmp/**"],
            }
        )

        self.assertEqual(plan.identifiers, ["saveConfig", "loadConfig"])
        self.assertEqual(plan.concept_terms, ["settings"])
        self.assertIn("tmp/**", plan.exclude_globs)

    def test_from_query_extracts_fallback_terms(self) -> None:
        plan = SearchPlan.from_query("点击保存后 refresh settings")
        values = [term.value for term in plan.terms()]

        self.assertIn("点击保存后", values)
        self.assertIn("refresh", values)
        self.assertIn("settings", values)

    def test_from_query_does_not_duplicate_single_term(self) -> None:
        plan = SearchPlan.from_query("settings")
        values = [term.value for term in plan.terms()]

        self.assertEqual(values, ["settings"])


if __name__ == "__main__":
    unittest.main()
