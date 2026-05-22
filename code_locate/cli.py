from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .commands import command_display
from .expand import expand_location
from .models import Match
from .ranking import rank_matches
from .search import MAX_FILE_BYTES, collect_matches
from .search_plan import SearchPlan
from .symbols import find_enclosing_symbol

MAX_TOP = 100
MAX_CONTEXT_RADIUS = 500
MAX_MATCHES_PER_TERM = 2_000
MAX_PARSE_LIMIT = 500


class CodeLocateArgumentParser(argparse.ArgumentParser):
    json_requested = False

    def error(self, message: str) -> None:
        if self.json_requested:
            _print_error_json("ArgumentError", message)
            raise SystemExit(2)
        super().error(message)


def main(argv: list[str] | None = None) -> int:
    raw_args = sys.argv[1:] if argv is None else argv
    CodeLocateArgumentParser.json_requested = "--json" in raw_args
    parser = build_parser()
    args: argparse.Namespace | None = None
    try:
        args = parser.parse_args(raw_args)
        return args.func(args)
    except BrokenPipeError:
        return 1
    except Exception as exc:
        if CodeLocateArgumentParser.json_requested or getattr(args, "json", False):
            _print_error_json(exc.__class__.__name__, str(exc))
        else:
            print(f"code-locate: error: {exc}", file=sys.stderr)
        return 2
    finally:
        CodeLocateArgumentParser.json_requested = False


def build_parser() -> CodeLocateArgumentParser:
    parser = CodeLocateArgumentParser(
        prog="code-locate",
        description="Locate likely code entry points from issue-derived search plans.",
    )
    parser.add_argument("--version", action="version", version=f"code-locate {__version__}")
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=CodeLocateArgumentParser,
    )

    query = subparsers.add_parser("query", help="rank likely code locations")
    query.add_argument("query", nargs="?", help="raw fallback query; agents should prefer --spec")
    query.add_argument("--spec", help="JSON search plan path")
    query.add_argument("--repo", default=".", help="repository root, default: current directory")
    query.add_argument("--top", type=_top_int, default=5, help="number of results, default: 5")
    query.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    query.add_argument("--max-matches-per-term", type=_max_matches_per_term_int, default=200)
    query.add_argument("--parse-limit", type=_parse_limit_int, default=50, help="max candidate files to parse")
    query.set_defaults(func=cmd_query)

    context = subparsers.add_parser("context", help="show source context around a location")
    context.add_argument("location", help="path:line or path:line:column")
    context.add_argument("--repo", default=".", help="repository root, default: current directory")
    context.add_argument("--radius", type=_radius_int, default=40, help="lines before and after")
    context.add_argument("--symbol", action="store_true", help="expand to enclosing symbol when possible")
    context.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    context.set_defaults(func=cmd_context)

    refs = subparsers.add_parser("refs", help="find references with grep-based search")
    refs.add_argument("symbol", help="identifier or phrase to search")
    refs.add_argument("--repo", default=".", help="repository root, default: current directory")
    refs.add_argument("--top", type=_top_int, default=20, help="max reference lines, default: 20")
    refs.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    refs.set_defaults(func=cmd_refs)

    expand = subparsers.add_parser("expand", help="expand a file or symbol into nearby dependency signals")
    expand.add_argument("target", help="path, path:line, or path:line:column")
    expand.add_argument("--repo", default=".", help="repository root, default: current directory")
    expand.add_argument("--scope", choices=["auto", "symbol", "file"], default="auto")
    expand.add_argument("--depth", type=_depth_int, default=1, help="import graph depth, default: 1, max: 3")
    expand.add_argument("--top", type=_top_int, default=20, help="max items per section, default: 20")
    expand.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    expand.set_defaults(func=cmd_expand)

    return parser


def _positive_int(value: str) -> int:
    return _bounded_int(value, minimum=1, maximum=None)


def _non_negative_int(value: str) -> int:
    return _bounded_int(value, minimum=0, maximum=None)


def _top_int(value: str) -> int:
    return _bounded_int(value, minimum=1, maximum=MAX_TOP, name="top")


def _radius_int(value: str) -> int:
    return _bounded_int(value, minimum=0, maximum=MAX_CONTEXT_RADIUS, name="radius")


def _max_matches_per_term_int(value: str) -> int:
    return _bounded_int(
        value,
        minimum=1,
        maximum=MAX_MATCHES_PER_TERM,
        name="max-matches-per-term",
    )


def _parse_limit_int(value: str) -> int:
    return _bounded_int(value, minimum=0, maximum=MAX_PARSE_LIMIT, name="parse-limit")


def _depth_int(value: str) -> int:
    number = _positive_int(value)
    if number > 3:
        raise argparse.ArgumentTypeError("must be <= 3")
    return number


def _parse_int(value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc


def _bounded_int(
    value: str,
    *,
    minimum: int,
    maximum: int | None,
    name: str = "value",
) -> int:
    number = _parse_int(value)
    if number < minimum:
        if name == "value":
            raise argparse.ArgumentTypeError(f"must be >= {minimum}")
        raise argparse.ArgumentTypeError(f"{name} must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise argparse.ArgumentTypeError(f"{name} must be <= {maximum}")
    return number


def _print_error_json(error_type: str, message: str) -> None:
    payload = {
        "error": {
            "type": error_type,
            "message": message,
        }
    }
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)


def cmd_query(args: argparse.Namespace) -> int:
    if not args.spec and not args.query:
        raise ValueError("query requires either a raw query or --spec")
    plan = SearchPlan.from_spec_path(args.spec) if args.spec else SearchPlan.from_query(args.query)
    matches = collect_matches(plan, args.repo, max_matches_per_term=args.max_matches_per_term)
    results = rank_matches(matches, args.repo, top=args.top, parse_limit=args.parse_limit)
    payload = {
        "query": plan.issue,
        "top": args.top,
        "terms": [term.__dict__ for term in plan.terms()],
        "match_count": len(matches),
        "result_count": len(results),
        "results": [candidate.to_dict(index) for index, candidate in enumerate(results, start=1)],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_query_human(payload)
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    rel_path, line = _parse_location(args.location)
    file_path = (repo / rel_path).resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"file does not exist: {rel_path}")
    if not _is_relative_to(file_path, repo):
        raise ValueError("location must stay inside repo")
    if not file_path.is_file():
        raise ValueError("context expects a file path")
    if file_path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"file is too large to read safely: {rel_path}")

    lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if line < 1 or line > len(lines):
        raise ValueError(f"line out of range: {line}")

    symbol = find_enclosing_symbol(file_path, line) if args.symbol else None
    if symbol:
        start = max(1, symbol.start_line)
        end = min(len(lines), symbol.end_line)
    else:
        start = max(1, line - args.radius)
        end = min(len(lines), line + args.radius)

    payload = {
        "path": rel_path,
        "line": line,
        "symbol": symbol.to_dict() if symbol else None,
        "context": [
            {"line": number, "text": lines[number - 1]}
            for number in range(start, end + 1)
        ],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_context_human(payload)
    return 0


def cmd_refs(args: argparse.Namespace) -> int:
    plan = SearchPlan(issue=args.symbol, identifiers=[args.symbol])
    matches = collect_matches(plan, args.repo, max_matches_per_term=args.top)
    payload = {
        "symbol": args.symbol,
        "result_count": min(args.top, len(matches)),
        "results": [_match_to_ref(match) for match in matches[: args.top]],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_refs_human(payload)
    return 0


def cmd_expand(args: argparse.Namespace) -> int:
    rel_path, line = _parse_expand_target(args.target)
    payload = expand_location(
        args.repo,
        rel_path,
        line,
        scope=args.scope,
        top=args.top,
        depth=args.depth,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_expand_human(payload)
    return 0


def _parse_location(location: str) -> tuple[str, int]:
    match = re.match(r"^(?P<path>.+):(?P<line>\d+)(?::\d+)?$", location)
    if not match:
        raise ValueError("location must look like path:line or path:line:column")
    return match.group("path"), int(match.group("line"))


def _parse_expand_target(target: str) -> tuple[str, int | None]:
    match = re.match(r"^(?P<path>.+?)(?::(?P<line>\d+)(?::\d+)?)?$", target)
    if not match:
        raise ValueError("target must look like path, path:line, or path:line:column")
    line = match.group("line")
    return match.group("path"), int(line) if line else None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _match_to_ref(match: Match) -> dict[str, Any]:
    return {
        "path": match.path,
        "line": match.line,
        "column": match.column,
        "text": match.text.strip(),
    }


def _print_query_human(payload: dict[str, Any]) -> None:
    print(f"query: {payload['query']}")
    print(f"matches: {payload['match_count']}  results: {payload['result_count']}")
    for result in payload["results"]:
        symbol = result["symbol"]
        symbol_text = ""
        if symbol:
            symbol_text = f"  {symbol['kind']} {symbol['name']}"
        print(
            f"\n{result['rank']}. {result['path']}:{result['lines']['start']}"
            f"-{result['lines']['end']}{symbol_text}"
        )
        print(f"   score: {result['score']}  confidence: {result['confidence']}")
        if result["matched_terms"]:
            print(f"   matched: {', '.join(result['matched_terms'])}")
        for evidence in result["evidence"][:3]:
            print(f"   L{evidence['line']}: {evidence['text']}")
        if result["suggested_next_steps"]:
            print(f"   next: {command_display(result['suggested_next_steps'][0])}")


def _print_context_human(payload: dict[str, Any]) -> None:
    symbol = payload.get("symbol")
    header = f"{payload['path']}:{payload['line']}"
    if symbol:
        header += f"  {symbol['kind']} {symbol['name']}"
    print(header)
    for item in payload["context"]:
        print(f"{item['line']:>5} | {item['text']}")


def _print_refs_human(payload: dict[str, Any]) -> None:
    print(f"symbol: {payload['symbol']}  results: {payload['result_count']}")
    for result in payload["results"]:
        print(f"{result['path']}:{result['line']}:{result['column']}: {result['text']}")


def _print_expand_human(payload: dict[str, Any]) -> None:
    target = payload["target"]
    symbol = target.get("symbol")
    header = f"target: {target['path']}"
    if target.get("line"):
        header += f":{target['line']}"
    if symbol:
        header += f"  {symbol['kind']} {symbol['name']}"
    else:
        header += "  file"
    print(header)
    analysis = payload.get("analysis", {})
    parser = analysis.get("symbol_parser", {})
    if parser:
        print(
            f"analysis: symbols={parser.get('backend', 'unknown')}"
            f" language={parser.get('language') or 'unknown'}"
        )

    _print_expand_section("imports", payload["imports"], _format_import_item)
    _print_expand_section("dependencies", payload["dependencies"], _format_path_item)
    _print_expand_section("dependents", payload["dependents"], _format_path_item)
    _print_expand_section("outgoing calls", payload["outgoing_calls"], _format_call_item)
    _print_expand_section("local callees", payload["local_callees"], _format_callee_item)
    _print_expand_section("imported callees", payload["imported_callees"], _format_imported_callee_item)
    _print_expand_section("incoming references", payload["incoming_references"], _format_reference_item)
    _print_expand_section("related files", payload["related_files"], _format_related_item)

    graph = payload["graph"]
    print(f"\ngraph: {len(graph['nodes'])} nodes, {len(graph['edges'])} edges, depth {graph['depth']}")
    if payload["suggested_next_steps"]:
        print("\nnext:")
        for step in payload["suggested_next_steps"][:6]:
            print(f"  {command_display(step)}")


def _print_expand_section(title: str, items: list[dict[str, Any]], formatter) -> None:
    print(f"\n{title}: {len(items)}")
    for item in items[:8]:
        print(f"  {formatter(item)}")


def _format_import_item(item: dict[str, Any]) -> str:
    target = f" -> {item['resolved_path']}" if item.get("resolved_path") else ""
    return f"L{item['line']} {item['kind']} {item['module']}{target}"


def _format_path_item(item: dict[str, Any]) -> str:
    line = f":{item['line']}" if item.get("line") else ""
    via = item.get("via") or item.get("module") or item.get("reason") or ""
    suffix = f"  via {via}" if via else ""
    return f"{item['path']}{line}{suffix}"


def _format_call_item(item: dict[str, Any]) -> str:
    return f"L{item['line']} {item['name']}()"


def _format_callee_item(item: dict[str, Any]) -> str:
    return f"{item['path']}:{item['line']} {item['kind']} {item['name']}  from L{item['called_from_line']}"


def _format_imported_callee_item(item: dict[str, Any]) -> str:
    target = f" -> {item['resolved_path']}" if item.get("resolved_path") else ""
    return f"L{item['line']} {item['name']}() from {item['module']}{target}"


def _format_reference_item(item: dict[str, Any]) -> str:
    symbol = item.get("enclosing_symbol")
    owner = f"  in {symbol['kind']} {symbol['name']}" if symbol else ""
    return f"{item['path']}:{item['line']}:{item['column']}{owner}: {item['text']}"


def _format_related_item(item: dict[str, Any]) -> str:
    return f"{item['path']}  {item['reason']}"


if __name__ == "__main__":
    raise SystemExit(main())
