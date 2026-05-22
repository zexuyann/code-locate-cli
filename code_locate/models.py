from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .commands import command_step


@dataclass(frozen=True)
class SearchTerm:
    value: str
    category: str
    weight: int


@dataclass(frozen=True)
class Match:
    path: str
    line: int
    column: int
    text: str
    term: SearchTerm


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    start_line: int
    end_line: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }


@dataclass
class Candidate:
    path: str
    start_line: int
    end_line: int
    symbol: Symbol | None
    score: int = 0
    matched_terms: set[str] = field(default_factory=set)
    categories: set[str] = field(default_factory=set)
    evidence: list[Match] = field(default_factory=list)

    def confidence(self) -> str:
        if self.score >= 180:
            return "high"
        if self.score >= 90:
            return "medium"
        return "low"

    def to_dict(self, rank: int) -> dict[str, Any]:
        next_steps = [
            command_step("code-locate", "context", f"{self.path}:{self.start_line}", "--radius", "80"),
            command_step("code-locate", "expand", f"{self.path}:{self.start_line}", "--depth", "1", "--top", "20"),
        ]
        if self.symbol and self.symbol.name:
            next_steps.append(command_step("code-locate", "refs", self.symbol.name, "--top", "20"))

        return {
            "rank": rank,
            "path": self.path,
            "lines": {
                "start": self.start_line,
                "end": self.end_line,
            },
            "symbol": self.symbol.to_dict() if self.symbol else None,
            "score": self.score,
            "confidence": self.confidence(),
            "matched_terms": sorted(self.matched_terms),
            "evidence": [
                {
                    "line": match.line,
                    "column": match.column,
                    "term": match.term.value,
                    "category": match.term.category,
                    "text": match.text.strip(),
                }
                for match in self.evidence[:5]
            ],
            "suggested_next_steps": next_steps,
        }
