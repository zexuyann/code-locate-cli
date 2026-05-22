from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .models import Match, SearchTerm
from .search_plan import SearchPlan


MAX_FILE_BYTES = 2_000_000
IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*$")
IDENTIFIER_BOUNDARY_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_$")


def collect_matches(
    plan: SearchPlan,
    repo: str | Path,
    max_matches_per_term: int = 200,
) -> list[Match]:
    repo_path = Path(repo).resolve()
    if not repo_path.exists():
        raise FileNotFoundError(f"repo does not exist: {repo_path}")
    if not repo_path.is_dir():
        raise ValueError(f"repo is not a directory: {repo_path}")
    terms = plan.terms()
    if not terms:
        return []
    if max_matches_per_term < 1:
        raise ValueError("max_matches_per_term must be >= 1")

    if shutil.which("rg"):
        return _collect_with_rg(plan, terms, repo_path, max_matches_per_term)
    return _collect_with_python(plan, terms, repo_path, max_matches_per_term)


def _collect_with_rg(
    plan: SearchPlan,
    terms: list[SearchTerm],
    repo: Path,
    max_matches_per_term: int,
) -> list[Match]:
    matches: list[Match] = []
    for term in terms:
        cmd = [
            "rg",
            "--json",
            "--line-number",
            "--column",
            "--fixed-strings",
            "--max-count",
            str(_per_file_match_limit(max_matches_per_term)),
        ]
        if _include_globs_allow_hidden(plan.include_globs):
            cmd.append("--hidden")
        if _case_insensitive(term):
            cmd.append("--ignore-case")
        for include_glob in plan.include_globs:
            cmd.extend(["--glob", include_glob])
        for exclude_glob in plan.exclude_globs:
            cmd.extend(["--glob", f"!{exclude_glob}"])
        cmd.extend(["--", term.value, "."])

        count = 0
        stopped_early = False
        with subprocess.Popen(
            cmd,
            cwd=repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as proc:
            assert proc.stdout is not None
            for line in proc.stdout:
                if not line:
                    continue
                match = _match_from_rg_line(line, term)
                if match is None:
                    continue
                matches.append(match)
                count += 1
                if count >= max_matches_per_term:
                    stopped_early = True
                    proc.terminate()
                    break
            stderr = proc.stderr.read() if proc.stderr is not None else ""
            returncode = proc.wait()
        if not stopped_early and returncode not in (0, 1):
            raise RuntimeError(stderr.strip() or f"rg failed for term {term.value!r}")
    return _dedupe_matches(matches)


def _collect_with_python(
    plan: SearchPlan,
    terms: list[SearchTerm],
    repo: Path,
    max_matches_per_term: int,
) -> list[Match]:
    matches: list[Match] = []
    files = list(_iter_files(repo, plan.include_globs, plan.exclude_globs))
    for term in terms:
        count = 0
        for file_path in files:
            if count >= max_matches_per_term:
                break
            try:
                if file_path.stat().st_size > MAX_FILE_BYTES:
                    continue
                lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            rel = file_path.relative_to(repo).as_posix()
            for index, line in enumerate(lines, start=1):
                column = _first_term_column(line, term)
                if column is None:
                    continue
                matches.append(
                    Match(path=rel, line=index, column=column, text=line, term=term)
                )
                count += 1
                if count >= max_matches_per_term:
                    break
    return _dedupe_matches(matches)


def _iter_files(repo: Path, include_globs: list[str], exclude_globs: list[str]):
    include_hidden = _include_globs_allow_hidden(include_globs)
    for root, dirs, files in os.walk(repo):
        root_path = Path(root)
        rel_root = root_path.relative_to(repo).as_posix()
        dirs[:] = [
            directory
            for directory in dirs
            if (
                (include_hidden or not _is_hidden_part(directory))
                and not _is_excluded(_join_posix(rel_root, directory), exclude_globs)
            )
        ]
        for file_name in files:
            file_path = root_path / file_name
            rel = file_path.relative_to(repo).as_posix()
            if not include_hidden and _path_has_hidden_part(rel):
                continue
            if include_globs and not any(fnmatch.fnmatch(rel, glob) for glob in include_globs):
                continue
            if _is_excluded(rel, exclude_globs):
                continue
            if not _file_stays_inside_repo(file_path, repo):
                continue
            yield file_path


def _is_excluded(path: str, exclude_globs: list[str]) -> bool:
    return any(_matches_glob(path, glob) for glob in exclude_globs)


def _matches_glob(path: str, glob: str) -> bool:
    if fnmatch.fnmatch(path, glob):
        return True
    if glob.endswith("/**"):
        prefix = glob[:-3]
        if prefix.startswith("**/"):
            suffix = prefix[3:]
            return path == suffix or path.endswith(f"/{suffix}") or f"/{suffix}/" in path
        return path == prefix or path.startswith(f"{prefix}/")
    return False


def _join_posix(root: str, name: str) -> str:
    if root in ("", "."):
        return name
    return f"{root}/{name}"


def _include_globs_allow_hidden(include_globs: list[str]) -> bool:
    return any(_glob_has_hidden_part(glob) for glob in include_globs)


def _glob_has_hidden_part(glob: str) -> bool:
    for part in glob.split("/"):
        if part.startswith(".") and part not in {".", ".."}:
            return True
    return False


def _path_has_hidden_part(path: str) -> bool:
    return any(_is_hidden_part(part) for part in path.split("/"))


def _is_hidden_part(part: str) -> bool:
    return part.startswith(".") and part not in {".", ".."}


def _file_stays_inside_repo(file_path: Path, repo: Path) -> bool:
    try:
        resolved = file_path.resolve()
    except OSError:
        return False
    return _is_relative_to(resolved, repo)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _normalize_relative_path(path: str) -> str:
    if path.startswith("./"):
        return path[2:]
    return path


def _dedupe_matches(matches: list[Match]) -> list[Match]:
    seen: set[tuple[str, int, str, str]] = set()
    result: list[Match] = []
    for match in matches:
        key = (match.path, match.line, match.term.category, match.term.value.lower())
        if key in seen:
            continue
        seen.add(key)
        result.append(match)
    return result


def _match_from_rg_line(line: str, term: SearchTerm) -> Match | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if event.get("type") != "match":
        return None
    data = event.get("data", {})
    raw_path = data.get("path", {}).get("text", "")
    if not raw_path:
        return None
    text = data.get("lines", {}).get("text", "")
    column = _first_term_column(text, term)
    if column is None:
        return None
    return Match(
        path=_normalize_relative_path(raw_path),
        line=int(data.get("line_number", 0) or 0),
        column=column,
        text=text,
        term=term,
    )


def _first_term_column(text: str, term: SearchTerm) -> int | None:
    if _uses_identifier_boundaries(term):
        return _first_identifier_column(text, term.value, case_insensitive=_case_insensitive(term))
    haystack = text.lower() if _case_insensitive(term) else text
    needle = term.value.lower() if _case_insensitive(term) else term.value
    column = haystack.find(needle)
    if column == -1:
        return None
    return column + 1


def _first_identifier_column(text: str, value: str, *, case_insensitive: bool) -> int | None:
    search_text = text.lower() if case_insensitive else text
    search_value = value.lower() if case_insensitive else value
    start = 0
    while True:
        index = search_text.find(search_value, start)
        if index == -1:
            return None
        before = text[index - 1] if index > 0 else ""
        after_index = index + len(value)
        after = text[after_index] if after_index < len(text) else ""
        if before not in IDENTIFIER_BOUNDARY_CHARS and after not in IDENTIFIER_BOUNDARY_CHARS:
            return index + 1
        start = index + 1


def _uses_identifier_boundaries(term: SearchTerm) -> bool:
    if not IDENTIFIER_RE.match(term.value):
        return False
    if term.category in {"identifier", "exact_phrase"}:
        return True
    return len(term.value) <= 4


def _case_insensitive(term: SearchTerm) -> bool:
    return term.category != "identifier"


def _per_file_match_limit(max_matches_per_term: int) -> int:
    return max(3, min(20, max_matches_per_term // 10 or 1))
