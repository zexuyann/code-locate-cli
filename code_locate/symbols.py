from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .models import Symbol


LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "c_sharp",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
}

SYMBOL_NODE_TYPES = {
    "class_declaration",
    "class_definition",
    "function_declaration",
    "function_definition",
    "method_definition",
    "method_declaration",
    "generator_function_declaration",
    "arrow_function",
    "function",
    "function_item",
    "impl_item",
    "method",
    "method_declaration",
    "lexical_declaration",
    "variable_declarator",
    "const_item",
    "struct_item",
    "enum_item",
    "interface_declaration",
    "type_alias_declaration",
}

NAME_NODE_TYPES = {
    "identifier",
    "property_identifier",
    "field_identifier",
    "type_identifier",
    "constant",
}


@dataclass(frozen=True)
class TreeSitterParse:
    source: str
    source_bytes: bytes
    language: str
    tree: Any
    root: Any


def find_enclosing_symbol(path: str | Path, line: int) -> Symbol | None:
    file_path = Path(path)
    if not file_path.exists():
        return None
    try:
        source = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    language = LANGUAGE_BY_SUFFIX.get(file_path.suffix.lower())
    if language:
        symbol = _find_with_tree_sitter(source, language, line)
        if symbol:
            return symbol
    return _find_with_heuristics(source, file_path.suffix.lower(), line)


def find_enclosing_symbol_from_list(symbols: list[Symbol], line: int) -> Symbol | None:
    best: Symbol | None = None
    for symbol in symbols:
        if line < symbol.start_line or line > symbol.end_line:
            continue
        if best is None or _symbol_span(symbol) <= _symbol_span(best):
            best = symbol
    return best


def list_symbols(path: str | Path) -> list[Symbol]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    try:
        source = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    language = LANGUAGE_BY_SUFFIX.get(file_path.suffix.lower())
    if language:
        symbols = _list_with_tree_sitter(source, language)
        if symbols:
            return symbols
    return _list_with_heuristics(source, file_path.suffix.lower())


def parser_status(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    language = LANGUAGE_BY_SUFFIX.get(file_path.suffix.lower())
    if not language:
        return {
            "language": None,
            "backend": "heuristic",
            "tree_sitter_available": False,
        }
    available = _get_parser(language) is not None
    return {
        "language": language,
        "backend": "tree-sitter" if available else "heuristic",
        "tree_sitter_available": available,
    }


def parse_tree_sitter(path: str | Path) -> TreeSitterParse | None:
    file_path = Path(path)
    language = LANGUAGE_BY_SUFFIX.get(file_path.suffix.lower())
    if language is None or not file_path.exists():
        return None
    try:
        source = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    parser = _get_parser(language)
    if parser is None:
        return None
    try:
        source_bytes = source.encode("utf-8")
        tree = _parse_source(parser, source, source_bytes)
        root = _tree_root(tree)
    except Exception:
        return None
    return TreeSitterParse(
        source=source,
        source_bytes=source_bytes,
        language=language,
        tree=tree,
        root=root,
    )


def _find_with_tree_sitter(source: str, language: str, line: int) -> Symbol | None:
    parser = _get_parser(language)
    if parser is None:
        return None
    try:
        source_bytes = source.encode("utf-8")
        tree = _parse_source(parser, source, source_bytes)
        root = _tree_root(tree)
    except Exception:
        return None

    target_row = max(line - 1, 0)
    best: Any | None = None
    stack = [root]
    while stack:
        node = stack.pop()
        start_row = _node_start_point(node)[0]
        end_row = _node_end_point(node)[0]
        if target_row < start_row or target_row > end_row:
            continue
        if _node_type(node) in SYMBOL_NODE_TYPES:
            if best is None or _node_span(node) <= _node_span(best):
                best = node
        stack.extend(reversed(_node_named_children(node)))

    if best is None:
        return None
    name = _node_name(best, source_bytes)
    if not name:
        name = _first_line_text(best, source_bytes)
    return Symbol(
        name=name,
        kind=_kind_for_node_type(_node_type(best)),
        start_line=_node_start_point(best)[0] + 1,
        end_line=_node_end_point(best)[0] + 1,
    )


def _list_with_tree_sitter(source: str, language: str) -> list[Symbol]:
    parser = _get_parser(language)
    if parser is None:
        return []
    try:
        source_bytes = source.encode("utf-8")
        tree = _parse_source(parser, source, source_bytes)
        root = _tree_root(tree)
    except Exception:
        return []

    symbols: list[Symbol] = []
    stack = [root]
    seen: set[tuple[str, int, int]] = set()
    while stack:
        node = stack.pop()
        node_type = _node_type(node)
        if node_type in SYMBOL_NODE_TYPES:
            name = _node_name(node, source_bytes)
            if name:
                symbol = Symbol(
                    name=name,
                    kind=_kind_for_node_type(node_type),
                    start_line=_node_start_point(node)[0] + 1,
                    end_line=_node_end_point(node)[0] + 1,
                )
                key = (symbol.name, symbol.start_line, symbol.end_line)
                if key not in seen:
                    seen.add(key)
                    symbols.append(symbol)
        stack.extend(reversed(_node_named_children(node)))

    return sorted(symbols, key=lambda item: (item.start_line, item.end_line, item.name))


@lru_cache(maxsize=32)
def _get_parser(language: str):
    try:
        from tree_sitter_language_pack import get_parser  # type: ignore

        return get_parser(language)
    except Exception:
        return None


def _parse_source(parser: Any, source: str, source_bytes: bytes) -> Any:
    return parser.parse(source)


def _tree_root(tree: Any) -> Any:
    return tree.root_node()


def _node_type(node: Any) -> str:
    return node.kind()


def _node_start_point(node: Any) -> tuple[int, int]:
    point = node.start_position()
    return (int(point.row), int(point.column))


def _node_end_point(node: Any) -> tuple[int, int]:
    point = node.end_position()
    return (int(point.row), int(point.column))


def _node_named_children(node: Any) -> list[Any]:
    count = int(node.named_child_count())
    return [node.named_child(index) for index in range(count)]


def _node_span(node: Any) -> int:
    start = _node_start_point(node)
    end = _node_end_point(node)
    return (end[0] - start[0]) * 10000 + (
        end[1] - start[1]
    )


def _symbol_span(symbol: Symbol) -> int:
    return symbol.end_line - symbol.start_line


def _node_name(node: Any, source_bytes: bytes) -> str:
    child_by_field_name = getattr(node, "child_by_field_name", None)
    if child_by_field_name:
        for field in ("name", "declarator", "property"):
            child = child_by_field_name(field)
            if child is not None:
                nested = _first_named_identifier(child, source_bytes)
                if nested:
                    return nested
                return _text(child, source_bytes)

    nested = _first_named_identifier(node, source_bytes)
    return nested or ""


def _first_named_identifier(node: Any, source_bytes: bytes) -> str:
    if _node_type(node) in NAME_NODE_TYPES:
        return _text(node, source_bytes)
    for child in _node_named_children(node):
        value = _first_named_identifier(child, source_bytes)
        if value:
            return value
    return ""


def _text(node: Any, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte() : node.end_byte()].decode("utf-8", errors="ignore").strip()


def _first_line_text(node: Any, source_bytes: bytes) -> str:
    return _text(node, source_bytes).splitlines()[0].strip()[:120]


def _kind_for_node_type(node_type: str) -> str:
    if "class" in node_type:
        return "class"
    if "method" in node_type:
        return "method"
    if "function" in node_type:
        return "function"
    if "struct" in node_type:
        return "struct"
    if "enum" in node_type:
        return "enum"
    if "interface" in node_type:
        return "interface"
    if "type_alias" in node_type:
        return "type"
    if "variable" in node_type or "lexical" in node_type or "const" in node_type:
        return "variable"
    return "symbol"


def _find_with_heuristics(source: str, suffix: str, line: int) -> Symbol | None:
    lines = source.splitlines()
    if not lines:
        return None
    index = min(max(line - 1, 0), len(lines) - 1)
    patterns = _patterns_for_suffix(suffix)
    if not patterns:
        patterns = _patterns_for_suffix(".js")

    for current in range(index, -1, -1):
        text = lines[current]
        for pattern, kind in patterns:
            match = pattern.match(text)
            if not match:
                continue
            name = match.group("name")
            return Symbol(
                name=name,
                kind=kind,
                start_line=current + 1,
                end_line=_estimate_symbol_end(lines, current, suffix),
            )
    return None


def _list_with_heuristics(source: str, suffix: str) -> list[Symbol]:
    lines = source.splitlines()
    patterns = _patterns_for_suffix(suffix)
    if not patterns:
        patterns = _patterns_for_suffix(".js")

    symbols: list[Symbol] = []
    seen_starts: set[int] = set()
    for index, text in enumerate(lines):
        for pattern, kind in patterns:
            match = pattern.match(text)
            if not match:
                continue
            if index in seen_starts:
                continue
            seen_starts.add(index)
            symbols.append(
                Symbol(
                    name=match.group("name"),
                    kind=kind,
                    start_line=index + 1,
                    end_line=_estimate_symbol_end(lines, index, suffix),
                )
            )
            break
    return symbols


def _patterns_for_suffix(suffix: str):
    common_js = [
        (
            re.compile(
                r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\b"
            ),
            "function",
        ),
        (
            re.compile(
                r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)"
            ),
            "function",
        ),
        (
            re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)\b"),
            "class",
        ),
        (
            re.compile(
                r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|async\s+|get\s+|set\s+)*(?P<name>[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*(?::[^{]+)?\{?\s*$"
            ),
            "method",
        ),
    ]
    if suffix == ".py":
        return [
            (re.compile(r"^\s*def\s+(?P<name>[A-Za-z_][\w]*)\s*\("), "function"),
            (re.compile(r"^\s*async\s+def\s+(?P<name>[A-Za-z_][\w]*)\s*\("), "function"),
            (re.compile(r"^\s*class\s+(?P<name>[A-Za-z_][\w]*)\b"), "class"),
        ]
    if suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        return common_js
    if suffix == ".go":
        return [
            (re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(?P<name>[A-Za-z_][\w]*)\s*\("), "function"),
            (re.compile(r"^\s*type\s+(?P<name>[A-Za-z_][\w]*)\s+struct\b"), "struct"),
            (re.compile(r"^\s*type\s+(?P<name>[A-Za-z_][\w]*)\s+interface\b"), "interface"),
        ]
    if suffix == ".rs":
        return [
            (re.compile(r"^\s*(?:pub\s+)?fn\s+(?P<name>[A-Za-z_][\w]*)\s*\("), "function"),
            (re.compile(r"^\s*(?:pub\s+)?struct\s+(?P<name>[A-Za-z_][\w]*)\b"), "struct"),
            (re.compile(r"^\s*(?:pub\s+)?enum\s+(?P<name>[A-Za-z_][\w]*)\b"), "enum"),
        ]
    if suffix in {".java", ".kt", ".kts", ".cs"}:
        return [
            (re.compile(r"^\s*(?:public|private|protected|internal|static|final|open|override|\s)+\s*class\s+(?P<name>[A-Za-z_][\w]*)\b"), "class"),
            (re.compile(r"^\s*(?:public|private|protected|internal|static|final|open|override|suspend|\s)+[\w<>\[\]?]+\s+(?P<name>[A-Za-z_][\w]*)\s*\("), "method"),
            (re.compile(r"^\s*fun\s+(?P<name>[A-Za-z_][\w]*)\s*\("), "function"),
        ]
    if suffix == ".rb":
        return [
            (re.compile(r"^\s*def\s+(?P<name>[A-Za-z_][\w!?=]*)\b"), "function"),
            (re.compile(r"^\s*class\s+(?P<name>[A-Za-z_:][\w:]*)\b"), "class"),
            (re.compile(r"^\s*module\s+(?P<name>[A-Za-z_:][\w:]*)\b"), "module"),
        ]
    return common_js


def _estimate_symbol_end(lines: list[str], start_index: int, suffix: str) -> int:
    if suffix == ".py":
        header_end = start_index
        paren_balance = 0
        for index in range(start_index, len(lines)):
            stripped = lines[index].strip()
            paren_balance += stripped.count("(") + stripped.count("[") + stripped.count("{")
            paren_balance -= stripped.count(")") + stripped.count("]") + stripped.count("}")
            if stripped.endswith(":") and paren_balance <= 0:
                header_end = index
                break

        base_indent = len(lines[start_index]) - len(lines[start_index].lstrip())
        for index in range(header_end + 1, len(lines)):
            line = lines[index]
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= base_indent:
                return index
        return len(lines)

    balance = 0
    seen_open = False
    for index in range(start_index, len(lines)):
        line = _strip_string_literals(lines[index])
        balance += line.count("{")
        if "{" in line:
            seen_open = True
        balance -= line.count("}")
        if seen_open and balance <= 0:
            return index + 1
    return min(len(lines), start_index + 1)


def _strip_string_literals(line: str) -> str:
    return re.sub(r"(['\"]).*?\1", "", line)
