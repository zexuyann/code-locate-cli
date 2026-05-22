from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .commands import command_display, command_step
from .models import Match, Symbol
from .search import MAX_FILE_BYTES, _iter_files, collect_matches
from .search_plan import DEFAULT_EXCLUDE_GLOBS, SearchPlan
from .symbols import (
    TreeSitterParse,
    _node_end_point,
    _node_named_children,
    _node_start_point,
    _node_type,
    _text,
    find_enclosing_symbol,
    list_symbols,
    parse_tree_sitter,
    parser_status,
)


LOCAL_IMPORT_PREFIXES = (".", "/")
JS_RESOLVE_SUFFIXES = (
    "",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".json",
)
INDEX_FILES = (
    "index.ts",
    "index.tsx",
    "index.js",
    "index.jsx",
    "index.mjs",
    "index.cjs",
)
CODE_SUFFIXES = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".kts",
    ".rb",
    ".php",
    ".cs",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".hpp",
}
CALL_RE = re.compile(r"(?<![\w$])(?P<name>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(")
TREE_IMPORT_NODE_TYPES = {
    "import_statement",
    "import_from_statement",
    "import_declaration",
    "export_statement",
}
TREE_CALL_NODE_TYPES = {
    "call",
    "call_expression",
    "new_expression",
}
CALL_KEYWORDS = {
    "assert",
    "catch",
    "class",
    "def",
    "elif",
    "for",
    "function",
    "if",
    "import",
    "new",
    "return",
    "switch",
    "while",
    "with",
}
NOISY_REFERENCE_NAMES = {
    "__init__",
    "__new__",
    "__str__",
    "__repr__",
}


@dataclass(frozen=True)
class ImportItem:
    line: int
    kind: str
    module: str
    names: tuple[str, ...]
    text: str
    resolved_path: str | None
    backend: str = "regex"

    def to_dict(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "kind": self.kind,
            "module": self.module,
            "names": list(self.names),
            "text": self.text.strip(),
            "resolved_path": self.resolved_path,
            "backend": self.backend,
        }


@dataclass(frozen=True)
class ClassInfo:
    name: str
    start_line: int
    end_line: int
    bases: tuple[str, ...]


def expand_location(
    repo: str | Path,
    rel_path: str,
    line: int | None = None,
    *,
    scope: str = "auto",
    top: int = 20,
    depth: int = 1,
) -> dict[str, Any]:
    if scope not in {"auto", "symbol", "file"}:
        raise ValueError("scope must be one of: auto, symbol, file")
    if depth < 1 or depth > 3:
        raise ValueError("depth must be between 1 and 3")

    repo_path = Path(repo).resolve()
    if not repo_path.exists():
        raise FileNotFoundError(f"repo does not exist: {repo_path}")
    if not repo_path.is_dir():
        raise ValueError(f"repo is not a directory: {repo_path}")
    file_path = (repo_path / rel_path).resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"file does not exist: {rel_path}")
    if not _is_relative_to(file_path, repo_path):
        raise ValueError("location must stay inside repo")
    if file_path.is_dir():
        raise ValueError("expand expects a file path, not a directory")

    rel_path = file_path.relative_to(repo_path).as_posix()
    lines = _safe_read_lines(file_path)
    if line is not None and (line < 1 or line > len(lines)):
        raise ValueError(f"line out of range: {line}")

    target_symbol = None
    if scope != "file" and line is not None:
        target_symbol = find_enclosing_symbol(file_path, line)
    if scope == "symbol" and target_symbol is None:
        raise ValueError("no enclosing symbol found at the requested location")

    if target_symbol:
        start_line = target_symbol.start_line
        end_line = min(target_symbol.end_line, len(lines))
        resolved_scope = "symbol"
    else:
        start_line = 1
        end_line = len(lines)
        resolved_scope = "file"

    parsed = parse_tree_sitter(file_path)
    imports = _parse_imports(file_path, repo_path, parsed)
    all_symbols = list_symbols(file_path)
    calls = _extract_calls(file_path, parsed, lines, file_path.suffix.lower(), start_line, end_line, top)
    dependencies = _dependencies_from_imports(imports, top)
    dependents = _find_dependents(repo_path, rel_path, top)
    local_callees = _find_local_callees(calls, all_symbols, rel_path, target_symbol, top)
    imported_callees = _find_imported_callees(calls, imports, top)
    imported_callees.extend(
        _find_tree_static_callees(
            repo_path,
            file_path,
            rel_path,
            parsed,
            calls,
            imports,
            target_symbol,
            top - len(imported_callees),
        )
    )
    incoming_references = _find_incoming_references(
        repo_path,
        target_symbol,
        rel_path,
        start_line,
        end_line,
        top,
    )
    related_files = _find_related_files(repo_path, rel_path, target_symbol, top)
    graph = _build_graph(
        repo_path,
        rel_path,
        target_symbol,
        dependencies,
        dependents,
        local_callees,
        imported_callees,
        incoming_references,
        depth,
        top,
    )
    suggested_next_steps = _suggest_next_steps(
        rel_path,
        line,
        target_symbol,
        dependencies,
        dependents,
        local_callees,
    )

    return {
        "target": {
            "path": rel_path,
            "line": line,
            "scope": resolved_scope,
            "lines": {"start": start_line, "end": end_line},
            "symbol": target_symbol.to_dict() if target_symbol else None,
        },
        "imports": [item.to_dict() for item in imports[:top]],
        "dependencies": dependencies,
        "dependents": dependents,
        "outgoing_calls": calls,
        "local_callees": local_callees,
        "imported_callees": imported_callees,
        "incoming_references": incoming_references,
        "related_files": related_files,
        "graph": graph,
        "analysis": {
            "symbol_parser": parser_status(file_path),
            "call_graph": (
                "tree-sitter syntax-static signals, not language-server precise"
                if parsed is not None
                else "static regex signals, not language-server precise"
            ),
        },
        "suggested_next_steps": suggested_next_steps,
    }


def _parse_imports(path: Path, repo: Path, parsed: TreeSitterParse | None = None) -> list[ImportItem]:
    if parsed is None:
        parsed = parse_tree_sitter(path)
    if parsed is not None:
        items = _parse_imports_with_tree_sitter(path, repo, parsed)
        if items or parsed.language in {"python", "javascript", "typescript", "tsx", "java", "go", "c", "cpp"}:
            return items

    lines = _safe_read_lines(path)
    suffix = path.suffix.lower()
    items: list[ImportItem] = []
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if suffix == ".py":
            items.extend(_parse_python_import(path, repo, index, line))
        elif suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
            items.extend(_parse_js_import(path, repo, index, line))
        elif suffix == ".go":
            items.extend(_parse_string_import(path, repo, index, line, "import"))
        elif suffix == ".rs":
            item = _parse_keyword_import(path, repo, index, line, "use", ";")
            if item:
                items.append(item)
        elif suffix in {".java", ".kt", ".kts", ".cs"}:
            item = _parse_keyword_import(path, repo, index, line, "import", ";")
            if item:
                items.append(item)
        elif suffix in {".c", ".h", ".cc", ".cpp", ".hpp"}:
            items.extend(_parse_c_include(path, repo, index, line))
    return items


def _parse_imports_with_tree_sitter(path: Path, repo: Path, parsed: TreeSitterParse) -> list[ImportItem]:
    items: list[ImportItem] = []
    for node in _walk_tree(parsed.root):
        node_type = _node_type(node)
        if node_type not in TREE_IMPORT_NODE_TYPES and not _is_c_include_node(parsed, node):
            continue
        line = _node_start_point(node)[0] + 1
        text = _node_text(parsed, node)
        normalized = _normalize_statement_text(text)
        if parsed.language == "python":
            for item in _parse_python_import_text(path, repo, line, normalized, backend="tree-sitter"):
                items.append(item)
        elif parsed.language in {"javascript", "typescript", "tsx"}:
            for item in _parse_js_import_text(path, repo, line, normalized, backend="tree-sitter"):
                items.append(item)
        elif parsed.language == "go":
            for item in _parse_go_import_text(path, repo, line, normalized, backend="tree-sitter"):
                items.append(item)
        elif parsed.language in {"c", "cpp"}:
            for item in _parse_c_include_text(path, repo, line, normalized, backend="tree-sitter"):
                items.append(item)
        elif parsed.language == "java":
            item = _parse_keyword_import_text(path, repo, line, normalized, "import", ";", backend="tree-sitter")
            if item:
                items.append(item)
    return _dedupe_imports(items)


def _parse_python_import(path: Path, repo: Path, line_number: int, line: str) -> list[ImportItem]:
    return _parse_python_import_text(path, repo, line_number, line, backend="regex")


def _parse_python_import_text(
    path: Path,
    repo: Path,
    line_number: int,
    text: str,
    *,
    backend: str,
) -> list[ImportItem]:
    stripped = _strip_inline_comment(text).strip()
    from_match = re.match(r"^from\s+(?P<module>[.\w]+)\s+import\s+(?P<names>.+)$", stripped)
    if from_match:
        module = from_match.group("module")
        names = _parse_import_names(from_match.group("names"))
        if module and set(module) == {"."} and names:
            module = f"{module}{names[0]}"
        return [
            ImportItem(
                line=line_number,
                kind="from",
                module=module,
                names=tuple(names),
                text=text,
                resolved_path=_resolve_import(path, repo, module),
                backend=backend,
            )
        ]

    import_match = re.match(r"^import\s+(?P<modules>.+)$", stripped)
    if not import_match:
        return []

    items: list[ImportItem] = []
    for raw_module in import_match.group("modules").split(","):
        module_part = raw_module.strip()
        if not module_part:
            continue
        module, names = _parse_python_module_alias(module_part)
        items.append(
            ImportItem(
                line=line_number,
                kind="import",
                module=module,
                names=tuple(names),
                text=text,
                resolved_path=_resolve_import(path, repo, module),
                backend=backend,
            )
        )
    return items


def _parse_js_import(path: Path, repo: Path, line_number: int, line: str) -> list[ImportItem]:
    return _parse_js_import_text(path, repo, line_number, line, backend="regex")


def _parse_js_import_text(
    path: Path,
    repo: Path,
    line_number: int,
    text: str,
    *,
    backend: str,
) -> list[ImportItem]:
    items: list[ImportItem] = []
    from_match = re.search(r"\b(?:import|export)\s+(?P<left>.*?)\s+from\s+['\"](?P<module>[^'\"]+)['\"]", text)
    if from_match:
        module = from_match.group("module")
        items.append(
            ImportItem(
                line=line_number,
                kind="import",
                module=module,
                names=tuple(_parse_js_import_names(from_match.group("left"))),
                text=text,
                resolved_path=_resolve_import(path, repo, module),
                backend=backend,
            )
        )

    bare_match = re.search(r"\bimport\s+['\"](?P<module>[^'\"]+)['\"]", text)
    if bare_match:
        module = bare_match.group("module")
        items.append(
            ImportItem(
                line=line_number,
                kind="import",
                module=module,
                names=(),
                text=text,
                resolved_path=_resolve_import(path, repo, module),
                backend=backend,
            )
        )

    for require_match in re.finditer(r"\brequire\s*\(\s*['\"](?P<module>[^'\"]+)['\"]\s*\)", text):
        module = require_match.group("module")
        items.append(
            ImportItem(
                line=line_number,
                kind="require",
                module=module,
                names=tuple(_parse_require_names(text[: require_match.start()])),
                text=text,
                resolved_path=_resolve_import(path, repo, module),
                backend=backend,
            )
        )

    for dynamic_match in re.finditer(r"\bimport\s*\(\s*['\"](?P<module>[^'\"]+)['\"]\s*\)", text):
        module = dynamic_match.group("module")
        items.append(
            ImportItem(
                line=line_number,
                kind="dynamic_import",
                module=module,
                names=(),
                text=text,
                resolved_path=_resolve_import(path, repo, module),
                backend=backend,
            )
        )

    return _dedupe_imports(items)


def _parse_string_import(path: Path, repo: Path, line_number: int, line: str, kind: str) -> list[ImportItem]:
    if kind not in line:
        return []
    items: list[ImportItem] = []
    for match in re.finditer(r"['\"](?P<module>[^'\"]+)['\"]", line):
        module = match.group("module")
        items.append(
            ImportItem(
                line=line_number,
                kind=kind,
                module=module,
                names=(),
                text=line,
                resolved_path=_resolve_import(path, repo, module),
            )
        )
    return items


def _parse_go_import_text(
    path: Path,
    repo: Path,
    line_number: int,
    text: str,
    *,
    backend: str,
) -> list[ImportItem]:
    if not text.strip().startswith("import"):
        return []
    items: list[ImportItem] = []
    for match in re.finditer(r"(?:(?P<alias>[A-Za-z_][\w.]*)\s+)?[\"'](?P<module>[^\"']+)[\"']", text):
        module = match.group("module")
        alias = match.group("alias")
        names = [alias] if alias and alias not in {"import"} else _last_module_parts(module)
        items.append(
            ImportItem(
                line=line_number,
                kind="import",
                module=module,
                names=tuple(names),
                text=text,
                resolved_path=_resolve_import(path, repo, module),
                backend=backend,
            )
        )
    return items


def _parse_keyword_import(
    path: Path,
    repo: Path,
    line_number: int,
    line: str,
    keyword: str,
    terminator: str,
) -> ImportItem | None:
    return _parse_keyword_import_text(path, repo, line_number, line, keyword, terminator, backend="regex")


def _parse_keyword_import_text(
    path: Path,
    repo: Path,
    line_number: int,
    text: str,
    keyword: str,
    terminator: str,
    *,
    backend: str,
) -> ImportItem | None:
    match = re.match(rf"^\s*{keyword}\s+(?P<module>.+?){re.escape(terminator)}?\s*$", text)
    if not match:
        return None
    module = match.group("module").strip()
    if not module:
        return None
    return ImportItem(
        line=line_number,
        kind=keyword,
        module=module,
        names=tuple(_last_module_parts(module)),
        text=text,
        resolved_path=_resolve_import(path, repo, module),
        backend=backend,
    )


def _parse_c_include(path: Path, repo: Path, line_number: int, line: str) -> list[ImportItem]:
    return _parse_c_include_text(path, repo, line_number, line, backend="regex")


def _parse_c_include_text(
    path: Path,
    repo: Path,
    line_number: int,
    text: str,
    *,
    backend: str,
) -> list[ImportItem]:
    match = re.match(r'^\s*#\s*include\s+(?P<bracket>[<"])(?P<module>[^>"]+)[>"]', text)
    if not match:
        return []
    module = match.group("module")
    resolved = None
    if match.group("bracket") == '"':
        resolved = _resolve_import(path, repo, module)
    return [
        ImportItem(
            line=line_number,
            kind="include",
            module=module,
            names=tuple(_last_module_parts(module)),
            text=text,
            resolved_path=resolved,
            backend=backend,
        )
    ]


def _resolve_import(current_file: Path, repo: Path, module: str) -> str | None:
    suffix = current_file.suffix.lower()
    candidates: list[Path] = []
    if suffix == ".py":
        candidates = _python_import_candidates(current_file, repo, module)
    elif suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        candidates = _js_import_candidates(current_file, repo, module)
    elif suffix in {".c", ".h", ".cc", ".cpp", ".hpp"}:
        candidates = [(current_file.parent / module).resolve(), (repo / module).resolve()]

    for candidate in candidates:
        if candidate.exists() and candidate.is_file() and _is_relative_to(candidate, repo):
            return candidate.relative_to(repo).as_posix()
    return None


def _python_import_candidates(current_file: Path, repo: Path, module: str) -> list[Path]:
    if not module:
        return []
    base: Path
    module_name: str
    if module.startswith("."):
        dot_count = len(module) - len(module.lstrip("."))
        module_name = module[dot_count:]
        base = current_file.parent
        for _ in range(max(0, dot_count - 1)):
            if base == repo:
                break
            base = base.parent
    else:
        base = repo
        module_name = module

    module_path = base / module_name.replace(".", "/") if module_name else base
    return [
        module_path.with_suffix(".py"),
        module_path / "__init__.py",
    ]


def _js_import_candidates(current_file: Path, repo: Path, module: str) -> list[Path]:
    if module.startswith("."):
        base = (current_file.parent / module).resolve()
    elif module.startswith("/"):
        base = (repo / module.lstrip("/")).resolve()
    else:
        return []

    candidates = [(Path(str(base) + suffix)).resolve() for suffix in JS_RESOLVE_SUFFIXES]
    candidates.extend((base / name).resolve() for name in INDEX_FILES)
    return candidates


def _dependencies_from_imports(imports: list[ImportItem], top: int) -> list[dict[str, Any]]:
    dependencies: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in imports:
        if not item.resolved_path or item.resolved_path in seen:
            continue
        seen.add(item.resolved_path)
        dependencies.append(
            {
                "path": item.resolved_path,
                "line": item.line,
                "via": item.module,
                "text": item.text.strip(),
                "next_step": command_step(
                    "code-locate",
                    "expand",
                    item.resolved_path,
                    "--depth",
                    "1",
                    "--top",
                    str(top),
                ),
            }
        )
        if len(dependencies) >= top:
            break
    return dependencies


def _find_dependents(repo: Path, target_rel: str, top: int) -> list[dict[str, Any]]:
    target_path = repo / target_rel
    dependents: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for file_path in _iter_code_files(repo):
        rel = file_path.relative_to(repo).as_posix()
        if rel == target_rel:
            continue
        for item in _parse_imports(file_path, repo):
            if item.resolved_path == target_rel or _raw_import_mentions_target(item, target_path, target_rel):
                key = (rel, item.line, item.module)
                if key in seen:
                    continue
                seen.add(key)
                dependents.append(
                    {
                        "path": rel,
                        "line": item.line,
                        "module": item.module,
                        "text": item.text.strip(),
                        "next_step": command_step(
                            "code-locate",
                            "expand",
                            f"{rel}:{item.line}",
                            "--depth",
                            "1",
                            "--top",
                            str(top),
                        ),
                    }
                )
                if len(dependents) >= top:
                    return dependents
    return dependents


def _extract_calls(
    path: Path,
    parsed: TreeSitterParse | None,
    lines: list[str],
    suffix: str,
    start_line: int,
    end_line: int,
    top: int,
) -> list[dict[str, Any]]:
    if parsed is None:
        parsed = parse_tree_sitter(path)
    if parsed is not None:
        return _extract_calls_with_tree_sitter(parsed, lines, start_line, end_line, top)

    calls: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for line_number in range(start_line, end_line + 1):
        if line_number < 1 or line_number > len(lines):
            continue
        text = _strip_string_literals(lines[line_number - 1])
        if _looks_like_import_line(text, suffix):
            continue
        for match in CALL_RE.finditer(text):
            if match.start("name") > 0 and text[match.start("name") - 1] == ".":
                continue
            name = match.group("name")
            base_name = name.split(".")[-1]
            if base_name in CALL_KEYWORDS or name in CALL_KEYWORDS:
                continue
            if _is_definition_line(text, base_name, suffix):
                continue
            key = (name, line_number, match.start("name") + 1)
            if key in seen:
                continue
            seen.add(key)
            calls.append(
                {
                    "name": name,
                    "base_name": base_name,
                    "line": line_number,
                    "column": match.start("name") + 1,
                    "text": lines[line_number - 1].strip(),
                }
            )
            if len(calls) >= top:
                return calls
    return calls


def _extract_calls_with_tree_sitter(
    parsed: TreeSitterParse,
    lines: list[str],
    start_line: int,
    end_line: int,
    top: int,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for node in _walk_tree(parsed.root):
        if _node_type(node) not in TREE_CALL_NODE_TYPES:
            continue
        line_number = _node_start_point(node)[0] + 1
        if line_number < start_line or line_number > end_line:
            continue
        function_node = _call_function_node(node)
        if function_node is None:
            continue
        name = _call_name(parsed, function_node)
        if not name:
            continue
        base_name = _call_base_name(name)
        if base_name in CALL_KEYWORDS or name in CALL_KEYWORDS:
            continue
        column = _node_start_point(function_node)[1] + 1
        key = (name, line_number, column)
        if key in seen:
            continue
        seen.add(key)
        calls.append(
            {
                "name": name,
                "base_name": base_name,
                "line": line_number,
                "column": column,
                "text": lines[line_number - 1].strip() if 1 <= line_number <= len(lines) else "",
                "backend": "tree-sitter",
            }
        )
        if len(calls) >= top:
            break
    return calls


def _find_local_callees(
    calls: list[dict[str, Any]],
    symbols: list[Symbol],
    rel_path: str,
    target_symbol: Symbol | None,
    top: int,
) -> list[dict[str, Any]]:
    by_name = {symbol.name: symbol for symbol in symbols}
    callees: list[dict[str, Any]] = []
    seen: set[str] = set()
    for call in calls:
        if call["name"].startswith("super()."):
            continue
        symbol = by_name.get(call["base_name"]) or by_name.get(call["name"])
        if symbol is None:
            continue
        if target_symbol and symbol.start_line == target_symbol.start_line:
            relation = "recursive"
        else:
            relation = "calls"
        key = f"{symbol.name}:{symbol.start_line}:{call['line']}"
        if key in seen:
            continue
        seen.add(key)
        callees.append(
            {
                "name": symbol.name,
                "kind": symbol.kind,
                "path": rel_path,
                "line": symbol.start_line,
                "called_from_line": call["line"],
                "relation": relation,
                "next_step": command_step(
                    "code-locate",
                    "expand",
                    f"{rel_path}:{symbol.start_line}",
                    "--depth",
                    "1",
                    "--top",
                    str(top),
                ),
            }
        )
        if len(callees) >= top:
            break
    return callees


def _find_imported_callees(
    calls: list[dict[str, Any]],
    imports: list[ImportItem],
    top: int,
) -> list[dict[str, Any]]:
    imported: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for call in calls:
        call_names = {
            call["name"],
            call["base_name"],
            call["name"].split(".")[0],
        }
        for item in imports:
            imported_names = set(item.names) or {_module_leaf(item.module)}
            if not call_names & imported_names:
                continue
            key = (item.module, call["name"], call["line"])
            if key in seen:
                continue
            seen.add(key)
            imported.append(
                {
                    "name": call["name"],
                    "line": call["line"],
                    "module": item.module,
                    "import_line": item.line,
                    "resolved_path": item.resolved_path,
                    "text": call["text"],
                    "next_step": (
                        command_step(
                            "code-locate",
                            "expand",
                            item.resolved_path,
                            "--depth",
                            "1",
                            "--top",
                            str(top),
                        )
                        if item.resolved_path
                        else None
                    ),
                }
            )
            if len(imported) >= top:
                return imported
    return imported


def _find_incoming_references(
    repo: Path,
    target_symbol: Symbol | None,
    target_rel: str,
    target_start: int,
    target_end: int,
    top: int,
) -> list[dict[str, Any]]:
    if target_symbol is None or not target_symbol.name:
        return []
    if _is_noisy_reference_name(target_symbol.name):
        return []
    plan = SearchPlan(issue=target_symbol.name, identifiers=[target_symbol.name])
    matches = collect_matches(plan, repo, max_matches_per_term=max(top * 5, top))
    references: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for match in matches:
        if match.path == target_rel and target_start <= match.line <= target_end:
            continue
        if not _looks_like_call_reference(target_symbol.name, match.text):
            continue
        key = (match.path, match.line, match.text.strip())
        if key in seen:
            continue
        seen.add(key)
        enclosing = find_enclosing_symbol(repo / match.path, match.line)
        references.append(_reference_to_dict(match, enclosing, top))
        if len(references) >= top:
            break
    return references


def _find_related_files(
    repo: Path,
    target_rel: str,
    target_symbol: Symbol | None,
    top: int,
) -> list[dict[str, Any]]:
    target = Path(target_rel)
    stem = target.stem.lower()
    symbol_name = target_symbol.name.lower() if target_symbol and not _is_noisy_reference_name(target_symbol.name) else ""
    related: list[dict[str, Any]] = []
    seen: set[str] = set()
    for file_path in _iter_code_files(repo):
        rel = file_path.relative_to(repo).as_posix()
        if rel == target_rel:
            continue
        lower = rel.lower()
        is_test = _looks_like_test_path(lower)
        shares_name = stem and _path_mentions_token(lower, stem)
        mentions_symbol = symbol_name and _path_mentions_token(lower, symbol_name)
        if not (is_test and (shares_name or mentions_symbol)):
            continue
        if rel in seen:
            continue
        seen.add(rel)
        related.append(
            {
                "path": rel,
                "reason": "test_or_spec_path",
                "next_step": command_step(
                    "code-locate",
                    "expand",
                    rel,
                    "--depth",
                    "1",
                    "--top",
                    str(top),
                ),
            }
        )
        if len(related) >= top:
            break
    return related


def _build_graph(
    repo: Path,
    target_rel: str,
    target_symbol: Symbol | None,
    dependencies: list[dict[str, Any]],
    dependents: list[dict[str, Any]],
    local_callees: list[dict[str, Any]],
    imported_callees: list[dict[str, Any]],
    incoming_references: list[dict[str, Any]],
    depth: int,
    top: int,
) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    edge_keys: set[tuple[str, str, str, int | None]] = set()

    target_file_id = f"file:{target_rel}"
    _add_node(nodes, target_file_id, "file", target_rel, None, None)
    target_id = target_file_id
    if target_symbol:
        target_id = _symbol_node_id(target_rel, target_symbol)
        _add_node(nodes, target_id, target_symbol.kind, target_rel, target_symbol.start_line, target_symbol.name)
        _add_edge(edges, edge_keys, target_id, target_file_id, "defined_in", None)

    for item in dependencies:
        node_id = f"file:{item['path']}"
        _add_node(nodes, node_id, "file", item["path"], None, None)
        _add_edge(edges, edge_keys, target_file_id, node_id, "imports", item.get("line"))

    for item in dependents:
        node_id = f"file:{item['path']}"
        _add_node(nodes, node_id, "file", item["path"], None, None)
        _add_edge(edges, edge_keys, node_id, target_file_id, "imports", item.get("line"))

    for item in local_callees:
        node_id = f"symbol:{item['path']}:{item['line']}:{item['name']}"
        _add_node(nodes, node_id, item["kind"], item["path"], item["line"], item["name"])
        _add_edge(edges, edge_keys, target_id, node_id, "calls", item.get("called_from_line"))

    for item in imported_callees:
        if not item.get("resolved_path"):
            continue
        node_id = f"file:{item['resolved_path']}"
        _add_node(nodes, node_id, "file", item["resolved_path"], None, None)
        _add_edge(edges, edge_keys, target_id, node_id, "calls_imported", item.get("line"))

    for item in incoming_references:
        enclosing = item.get("enclosing_symbol")
        if enclosing:
            node_id = f"symbol:{item['path']}:{enclosing['start_line']}:{enclosing['name']}"
            _add_node(nodes, node_id, enclosing["kind"], item["path"], enclosing["start_line"], enclosing["name"])
        else:
            node_id = f"location:{item['path']}:{item['line']}"
            _add_node(nodes, node_id, "location", item["path"], item["line"], None)
        _add_edge(edges, edge_keys, node_id, target_id, "references", item.get("line"))

    if depth > 1:
        _extend_import_graph(repo, target_rel, depth, top, nodes, edges, edge_keys)

    return {
        "depth": depth,
        "nodes": list(nodes.values()),
        "edges": edges,
    }


def _extend_import_graph(
    repo: Path,
    target_rel: str,
    depth: int,
    top: int,
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    edge_keys: set[tuple[str, str, str, int | None]],
) -> None:
    queue: list[tuple[str, int]] = [(target_rel, 0)]
    seen: set[str] = {target_rel}
    while queue:
        rel, current_depth = queue.pop(0)
        if current_depth >= depth:
            continue
        imports = _parse_imports(repo / rel, repo)
        followed = 0
        for item in imports:
            if not item.resolved_path:
                continue
            source_id = f"file:{rel}"
            target_id = f"file:{item.resolved_path}"
            _add_node(nodes, source_id, "file", rel, None, None)
            _add_node(nodes, target_id, "file", item.resolved_path, None, None)
            _add_edge(edges, edge_keys, source_id, target_id, "imports", item.line)
            followed += 1
            if item.resolved_path not in seen:
                seen.add(item.resolved_path)
                queue.append((item.resolved_path, current_depth + 1))
            if followed >= top:
                break


def _suggest_next_steps(
    target_rel: str,
    line: int | None,
    target_symbol: Symbol | None,
    dependencies: list[dict[str, Any]],
    dependents: list[dict[str, Any]],
    local_callees: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    location = f"{target_rel}:{line}" if line is not None else target_rel
    steps = [
        command_step("code-locate", "context", f"{target_rel}:{line or 1}", "--radius", "80", "--symbol"),
        command_step("code-locate", "expand", location, "--depth", "2", "--top", "20", "--json"),
    ]
    if target_symbol:
        if not _is_noisy_reference_name(target_symbol.name):
            steps.append(command_step("code-locate", "refs", target_symbol.name, "--top", "20", "--json"))
    for collection in (local_callees, dependencies, dependents):
        for item in collection[:2]:
            next_step = item.get("next_step")
            if next_step:
                steps.append(_with_json(next_step))
    return _dedupe_steps(steps)[:10]


def _reference_to_dict(match: Match, enclosing: Symbol | None, top: int) -> dict[str, Any]:
    return {
        "path": match.path,
        "line": match.line,
        "column": match.column,
        "text": match.text.strip(),
        "enclosing_symbol": enclosing.to_dict() if enclosing else None,
        "next_step": command_step(
            "code-locate",
            "expand",
            f"{match.path}:{match.line}",
            "--depth",
            "1",
            "--top",
            str(top),
        ),
    }


def _find_tree_static_callees(
    repo: Path,
    file_path: Path,
    rel_path: str,
    parsed: TreeSitterParse | None,
    calls: list[dict[str, Any]],
    imports: list[ImportItem],
    target_symbol: Symbol | None,
    top: int,
) -> list[dict[str, Any]]:
    if top <= 0 or parsed is None or parsed.language != "python":
        return []
    class_info = _find_enclosing_class(parsed, target_symbol.start_line if target_symbol else 1)
    import_aliases = _import_aliases(imports)
    resolved: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()

    for call in calls:
        target_class: str | None = None
        target_import: ImportItem | None = None
        relation = "tree_sitter_static"
        if call["name"].startswith("super().") and class_info and class_info.bases:
            target_class = _leaf_name(class_info.bases[0])
            target_import = import_aliases.get(target_class)
            relation = "super_call"
        elif "." in call["name"] and "()" not in call["name"]:
            owner, _method = call["name"].split(".", 1)
            target_class = owner
            target_import = import_aliases.get(owner)
            relation = "imported_method"

        if target_class is None or target_import is None or not target_import.resolved_path:
            continue

        symbol = _find_symbol_in_class(repo / target_import.resolved_path, target_class, call["base_name"])
        key = (target_import.resolved_path, call["base_name"], call["line"])
        if key in seen:
            continue
        seen.add(key)
        resolved.append(
            {
                "name": call["base_name"],
                "line": call["line"],
                "module": target_import.module,
                "import_line": target_import.line,
                "resolved_path": target_import.resolved_path,
                "text": call["text"],
                "relation": relation,
                "confidence": "medium",
                "resolved_symbol": symbol.to_dict() if symbol else None,
                "next_step": (
                    command_step(
                        "code-locate",
                        "expand",
                        f"{target_import.resolved_path}:{symbol.start_line}",
                        "--depth",
                        "1",
                        "--top",
                        str(top),
                    )
                    if symbol
                    else command_step(
                        "code-locate",
                        "expand",
                        target_import.resolved_path,
                        "--depth",
                        "1",
                        "--top",
                        str(top),
                    )
                ),
            }
        )
        if len(resolved) >= top:
            break
    return resolved


def _iter_code_files(repo: Path):
    for file_path in _iter_files(repo, [], DEFAULT_EXCLUDE_GLOBS):
        if file_path.suffix.lower() in CODE_SUFFIXES:
            try:
                if file_path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield file_path


def _safe_read_lines(path: Path) -> list[str]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []


def _walk_tree(node: Any):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(_node_named_children(current)))


def _node_text(parsed: TreeSitterParse, node: Any) -> str:
    return _text(node, parsed.source_bytes)


def _normalize_statement_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _is_c_include_node(parsed: TreeSitterParse, node: Any) -> bool:
    return parsed.language in {"c", "cpp"} and _node_type(node) == "preproc_include"


def _call_function_node(call_node: Any) -> Any | None:
    child_by_field_name = getattr(call_node, "child_by_field_name", None)
    if child_by_field_name:
        function = child_by_field_name("function")
        if function is not None:
            return function
    children = _node_named_children(call_node)
    return children[0] if children else None


def _call_name(parsed: TreeSitterParse, function_node: Any) -> str:
    name = _normalize_statement_text(_node_text(parsed, function_node))
    if name.startswith("new "):
        name = name[4:].strip()
    return name


def _call_base_name(name: str) -> str:
    if "." not in name:
        return name
    return name.rsplit(".", 1)[-1]


def _class_infos(parsed: TreeSitterParse) -> list[ClassInfo]:
    classes: list[ClassInfo] = []
    for node in _walk_tree(parsed.root):
        if _node_type(node) != "class_definition":
            continue
        name_node = node.child_by_field_name("name")
        if name_node is None:
            continue
        super_node = node.child_by_field_name("superclasses")
        bases = _parse_base_names(_node_text(parsed, super_node)) if super_node is not None else []
        classes.append(
            ClassInfo(
                name=_node_text(parsed, name_node),
                start_line=_node_start_point(node)[0] + 1,
                end_line=_node_end_point(node)[0] + 1,
                bases=tuple(bases),
            )
        )
    return classes


def _find_enclosing_class(parsed: TreeSitterParse, line: int) -> ClassInfo | None:
    best: ClassInfo | None = None
    for class_info in _class_infos(parsed):
        if line < class_info.start_line or line > class_info.end_line:
            continue
        if best is None or (class_info.end_line - class_info.start_line) <= (best.end_line - best.start_line):
            best = class_info
    return best


def _parse_base_names(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]
    bases: list[str] = []
    for part in raw.split(","):
        base = part.strip()
        if not base:
            continue
        base = re.sub(r"\(.*$", "", base).strip()
        base = re.sub(r"\[.*$", "", base).strip()
        if base:
            bases.append(base)
    return bases


def _leaf_name(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _import_aliases(imports: list[ImportItem]) -> dict[str, ImportItem]:
    aliases: dict[str, ImportItem] = {}
    for item in imports:
        for name in item.names:
            aliases.setdefault(name, item)
        aliases.setdefault(_module_leaf(item.module), item)
    return aliases


def _find_symbol_in_class(path: Path, class_name: str, symbol_name: str) -> Symbol | None:
    symbols = list_symbols(path)
    class_symbol = next(
        (
            symbol
            for symbol in symbols
            if symbol.kind == "class" and symbol.name == class_name
        ),
        None,
    )
    if class_symbol is not None:
        for symbol in symbols:
            if symbol.name == symbol_name and class_symbol.start_line <= symbol.start_line <= class_symbol.end_line:
                return symbol
    for symbol in symbols:
        if symbol.name == symbol_name:
            return symbol
    return None


def _parse_import_names(raw: str) -> list[str]:
    raw = raw.replace("(", "").replace(")", "").strip()
    names: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part or part == "*":
            continue
        names.extend(_names_with_alias(part))
    return _dedupe_strings(names)


def _parse_python_module_alias(raw: str) -> tuple[str, list[str]]:
    parts = raw.split()
    module = parts[0]
    names = [module.split(".")[0]]
    if len(parts) >= 3 and parts[-2] == "as":
        names.append(parts[-1])
    return module, _dedupe_strings(names)


def _parse_js_import_names(left: str) -> list[str]:
    left = left.strip()
    if left.startswith("type "):
        left = left[5:].strip()
    names: list[str] = []
    namespace_match = re.search(r"\*\s+as\s+(?P<name>[A-Za-z_$][\w$]*)", left)
    if namespace_match:
        names.append(namespace_match.group("name"))
    brace_match = re.search(r"\{(?P<names>[^}]+)\}", left)
    if brace_match:
        for part in brace_match.group("names").split(","):
            names.extend(_names_with_alias(part.strip()))
    default_part = re.split(r"[,{]", left, maxsplit=1)[0].strip()
    if re.match(r"^[A-Za-z_$][\w$]*$", default_part):
        names.append(default_part)
    return _dedupe_strings(names)


def _parse_require_names(left: str) -> list[str]:
    match = re.search(r"(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*$", left)
    if match:
        return [match.group("name")]
    return []


def _names_with_alias(raw: str) -> list[str]:
    if not raw:
        return []
    parts = raw.split()
    if len(parts) >= 3 and parts[-2] in {"as", "AS"}:
        return [parts[0], parts[-1]]
    return [parts[0]]


def _last_module_parts(module: str) -> list[str]:
    cleaned = module.strip().strip(";").strip('"').strip("'")
    parts = re.split(r"[./:]+|::", cleaned)
    return [part for part in parts[-2:] if part]


def _module_leaf(module: str) -> str:
    parts = _last_module_parts(module)
    return parts[-1] if parts else module


def _raw_import_mentions_target(item: ImportItem, target_path: Path, target_rel: str) -> bool:
    target = Path(target_rel)
    module = item.module.strip().strip('"').strip("'").lower()
    variants = {
        target.stem.lower(),
        target.with_suffix("").as_posix().lower(),
        target.with_suffix("").as_posix().replace("/", ".").lower(),
        target_path.stem.lower(),
    }
    variants = {variant for variant in variants if variant}
    return any(
        module == variant
        or module.endswith(f".{variant}")
        or module.endswith(f"/{variant}")
        for variant in variants
    )


def _is_noisy_reference_name(name: str) -> bool:
    return name in NOISY_REFERENCE_NAMES


def _path_mentions_token(path: str, token: str) -> bool:
    return token in re.split(r"[^a-z0-9_]+|_", path)


def _looks_like_call_reference(symbol_name: str, text: str) -> bool:
    escaped = re.escape(symbol_name)
    return bool(re.search(rf"(?<![\w$])(?:[A-Za-z_$][\w$]*\.)?{escaped}\s*\(", text))


def _looks_like_import_line(text: str, suffix: str) -> bool:
    stripped = text.strip()
    if suffix == ".py":
        return stripped.startswith("import ") or stripped.startswith("from ")
    if suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        return stripped.startswith("import ") or " require(" in stripped
    return stripped.startswith("#include") or stripped.startswith("use ") or stripped.startswith("import ")


def _is_definition_line(text: str, name: str, suffix: str) -> bool:
    escaped = re.escape(name)
    if suffix == ".py":
        return bool(re.match(rf"^\s*(?:async\s+)?def\s+{escaped}\s*\(", text))
    if suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        return bool(
            re.match(rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+{escaped}\s*\(", text)
            or re.match(rf"^\s*(?:export\s+)?(?:const|let|var)\s+{escaped}\s*=", text)
            or re.match(rf"^\s*(?:export\s+)?(?:default\s+)?class\s+{escaped}\b", text)
        )
    return bool(re.search(rf"\b{escaped}\s*\([^)]*\)\s*(?:\{{|:)?\s*$", text))


def _strip_inline_comment(line: str) -> str:
    return line.split("#", 1)[0]


def _strip_string_literals(line: str) -> str:
    return re.sub(r"(['\"]).*?\1", r"\1\1", line)


def _looks_like_test_path(path: str) -> bool:
    return (
        "/test/" in path
        or "/tests/" in path
        or path.startswith("test/")
        or path.startswith("tests/")
        or ".test." in path
        or ".spec." in path
        or path.endswith("_test.py")
        or path.endswith("_test.go")
    )


def _dedupe_imports(items: list[ImportItem]) -> list[ImportItem]:
    seen: set[tuple[int, str, str]] = set()
    result: list[ImportItem] = []
    for item in items:
        key = (item.line, item.kind, item.module)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _with_json(step: object) -> dict[str, Any]:
    if isinstance(step, dict):
        argv = step.get("argv")
        if isinstance(argv, list):
            values = [str(item) for item in argv]
            if "--json" not in values:
                values.append("--json")
            return command_step(*values)
    text = str(step)
    return command_step(text, "--json")


def _dedupe_steps(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for value in values:
        key = command_display(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _add_node(
    nodes: dict[str, dict[str, Any]],
    node_id: str,
    kind: str,
    path: str,
    line: int | None,
    name: str | None,
) -> None:
    if node_id in nodes:
        return
    nodes[node_id] = {
        "id": node_id,
        "kind": kind,
        "path": path,
        "line": line,
        "name": name,
    }


def _add_edge(
    edges: list[dict[str, Any]],
    seen: set[tuple[str, str, str, int | None]],
    source: str,
    target: str,
    relation: str,
    line: int | None,
) -> None:
    key = (source, target, relation, line)
    if key in seen:
        return
    seen.add(key)
    edges.append({"source": source, "target": target, "relation": relation, "line": line})


def _symbol_node_id(path: str, symbol: Symbol) -> str:
    return f"symbol:{path}:{symbol.start_line}:{symbol.name}"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
