from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import SearchTerm


DEFAULT_EXCLUDE_GLOBS = [
    ".git/**",
    ".cache/**",
    ".mypy_cache/**",
    ".pytest_cache/**",
    ".ruff_cache/**",
    ".tox/**",
    ".nox/**",
    ".venv/**",
    "venv/**",
    "env/**",
    "node_modules/**",
    "dist/**",
    "build/**",
    "coverage/**",
    ".next/**",
    ".nuxt/**",
    "target/**",
    "vendor/**",
    "**/__pycache__/**",
    "**/site-packages/**",
    ".env",
    ".env.*",
    ".env/**",
    ".env.*/**",
    "**/.env",
    "**/.env.*",
    "**/.env/**",
    "**/.env.*/**",
    "*.pem",
    "*.key",
    "**/*.pem",
    "**/*.key",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "**/id_rsa",
    "**/id_dsa",
    "**/id_ecdsa",
    "**/id_ed25519",
]

TERM_WEIGHTS = {
    "exact_phrase": 100,
    "identifier": 50,
    "api_term": 60,
    "storage_term": 35,
    "framework_term": 30,
    "concept_term": 25,
}


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of strings")
    strings: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field_name} must be a list of strings")
        item = item.strip()
        if item:
            strings.append(item)
    return _dedupe(strings)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


@dataclass
class SearchPlan:
    issue: str = ""
    exact_phrases: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    concept_terms: list[str] = field(default_factory=list)
    api_terms: list[str] = field(default_factory=list)
    storage_terms: list[str] = field(default_factory=list)
    framework_terms: list[str] = field(default_factory=list)
    include_globs: list[str] = field(default_factory=list)
    exclude_globs: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_GLOBS))

    @classmethod
    def from_spec_path(cls, path: str | Path) -> "SearchPlan":
        spec_path = Path(path)
        data = json.loads(spec_path.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SearchPlan":
        if not isinstance(data, dict):
            raise ValueError("search plan must be a JSON object")
        issue = data.get("issue", "")
        if issue is None:
            issue = ""
        if not isinstance(issue, str):
            raise ValueError("issue must be a string")

        exclude_globs = DEFAULT_EXCLUDE_GLOBS + _string_list(data.get("exclude_globs"), "exclude_globs")
        return cls(
            issue=issue.strip(),
            exact_phrases=_string_list(data.get("exact_phrases"), "exact_phrases"),
            identifiers=_string_list(data.get("identifiers"), "identifiers"),
            concept_terms=_string_list(data.get("concept_terms"), "concept_terms"),
            api_terms=_string_list(data.get("api_terms"), "api_terms"),
            storage_terms=_string_list(data.get("storage_terms"), "storage_terms"),
            framework_terms=_string_list(data.get("framework_terms"), "framework_terms"),
            include_globs=_string_list(data.get("include_globs"), "include_globs"),
            exclude_globs=_dedupe(exclude_globs),
        )

    @classmethod
    def from_query(cls, query: str) -> "SearchPlan":
        query = query.strip()
        latin_terms = re.findall(r"[A-Za-z_$][A-Za-z0-9_.$:/-]*", query)
        cjk_terms = re.findall(r"[\u4e00-\u9fff]{2,}", query)
        concept_terms = _dedupe(latin_terms + cjk_terms)
        if len(concept_terms) == 1 and concept_terms[0].lower() == query.lower():
            concept_terms = []
        exact_phrases = [query] if query else []
        return cls(issue=query, exact_phrases=exact_phrases, concept_terms=concept_terms)

    def terms(self) -> list[SearchTerm]:
        grouped = [
            ("exact_phrase", self.exact_phrases),
            ("identifier", self.identifiers),
            ("api_term", self.api_terms),
            ("storage_term", self.storage_terms),
            ("framework_term", self.framework_terms),
            ("concept_term", self.concept_terms),
        ]
        terms: list[SearchTerm] = []
        seen: set[tuple[str, str]] = set()
        for category, values in grouped:
            for value in values:
                key = (category, value.lower())
                if key in seen:
                    continue
                seen.add(key)
                terms.append(SearchTerm(value=value, category=category, weight=TERM_WEIGHTS[category]))
        return terms

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue": self.issue,
            "exact_phrases": self.exact_phrases,
            "identifiers": self.identifiers,
            "concept_terms": self.concept_terms,
            "api_terms": self.api_terms,
            "storage_terms": self.storage_terms,
            "framework_terms": self.framework_terms,
            "include_globs": self.include_globs,
            "exclude_globs": self.exclude_globs,
        }
