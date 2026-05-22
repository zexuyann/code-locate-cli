from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from .models import Candidate, Match
from .symbols import find_enclosing_symbol_from_list, list_symbols


LOW_VALUE_PATH_PARTS = {
    "node_modules",
    "dist",
    "build",
    "coverage",
    "vendor",
    "target",
    ".next",
    ".nuxt",
}


def rank_matches(
    matches: list[Match],
    repo: str | Path,
    top: int,
    parse_limit: int = 50,
) -> list[Candidate]:
    repo_path = Path(repo).resolve()
    parseable_files = _top_files(matches, parse_limit)
    symbols_by_file = {
        path: list_symbols(repo_path / path)
        for path in parseable_files
    }
    candidates: dict[tuple[str, int, str], Candidate] = {}

    for match in matches:
        symbol = None
        if match.path in parseable_files:
            symbol = find_enclosing_symbol_from_list(symbols_by_file.get(match.path, []), match.line)

        start_line = symbol.start_line if symbol else match.line
        end_line = symbol.end_line if symbol else match.line
        symbol_key = symbol.name if symbol else f"line:{match.line}"
        key = (match.path, start_line, symbol_key)
        if key not in candidates:
            candidates[key] = Candidate(
                path=match.path,
                start_line=start_line,
                end_line=end_line,
                symbol=symbol,
            )
        candidate = candidates[key]
        candidate.score += match.term.weight
        candidate.matched_terms.add(match.term.value)
        candidate.categories.add(match.term.category)
        candidate.evidence.append(match)

    for candidate in candidates.values():
        candidate.score += _candidate_bonus(candidate)
        candidate.evidence.sort(key=lambda item: (item.line, -item.term.weight, item.term.value))

    ranked = sorted(
        candidates.values(),
        key=lambda candidate: (
            candidate.score,
            len(candidate.categories),
            len(candidate.matched_terms),
            -candidate.start_line,
        ),
        reverse=True,
    )
    return ranked[:top]


def _top_files(matches: list[Match], limit: int) -> set[str]:
    counts = Counter(match.path for match in matches)
    return {path for path, _count in counts.most_common(limit)}


def _candidate_bonus(candidate: Candidate) -> int:
    score = 0
    path_lower = candidate.path.lower()
    symbol_lower = candidate.symbol.name.lower() if candidate.symbol else ""
    terms_lower = [term.lower() for term in candidate.matched_terms]

    if len(candidate.categories) >= 2:
        score += 30
    if len(candidate.matched_terms) >= 3:
        score += 20
    if any(term and term in path_lower for term in terms_lower):
        score += 40
    if symbol_lower and any(term and term in symbol_lower for term in terms_lower):
        score += 40
    if _looks_like_test(candidate.path):
        score -= 10
    if _is_low_value_path(candidate.path):
        score -= 50
    return score


def _looks_like_test(path: str) -> bool:
    lower = path.lower()
    return (
        "/test/" in lower
        or "/tests/" in lower
        or lower.endswith(".test.ts")
        or lower.endswith(".test.tsx")
        or lower.endswith(".spec.ts")
        or lower.endswith(".spec.tsx")
        or lower.endswith("_test.go")
        or lower.endswith("_test.py")
    )


def _is_low_value_path(path: str) -> bool:
    parts = {part.lower() for part in Path(path).parts}
    return bool(parts & LOW_VALUE_PATH_PARTS)


def group_matches_by_file(matches: list[Match]) -> dict[str, list[Match]]:
    grouped: dict[str, list[Match]] = defaultdict(list)
    for match in matches:
        grouped[match.path].append(match)
    return dict(grouped)
