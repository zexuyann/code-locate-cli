# code-locate-cli

![CI](https://img.shields.io/badge/CI-pending-lightgrey.svg)
![Python](https://img.shields.io/badge/python-%3E%3D3.10-blue.svg)
![License](https://img.shields.io/badge/license-MIT-blue.svg)

A deterministic, agent-facing CLI for locating likely code entry points from bug reports, test failures, stack traces, and behavior issues.

[English](#features) | [中文](#功能特性)

## Features

- **Search-plan retrieval** - run structured issue-derived searches with `code-locate query --spec ...`
- **Grep-first core** - uses `ripgrep` when available, with a Python fallback
- **Symbol shaping** - groups matches around functions, methods, classes, and nearby symbols
- **Source context** - inspect `path:line` ranges with optional enclosing-symbol expansion
- **Dependency expansion** - follow imports, dependents, local callees, imported callees, incoming references, and related tests
- **Reference search** - grep-based `refs` command for quick caller-like exploration
- **Structured JSON** - machine-readable output for agents, including evidence and next-step `argv`
- **Safe defaults** - skips hidden files by default and excludes generated, vendored, cache, `.env*`, and private-key paths
- **Bounded execution** - explicit caps for `--top`, `--radius`, `--depth`, match collection, and parse limits
- **No LLM inside** - query rewriting and root-cause judgment stay in the agent or skill layer

> **AI Agent Tip:** Prefer `--spec` and `--json`. Execute `suggested_next_steps[].argv`; use `display` only for logs or copy/paste.

## Installation

Recommended:

```bash
uv tool install git+https://github.com/zexuyann/code-locate-cli.git
```

Upgrade:

```bash
uv tool upgrade code-locate
```

Alternative:

```bash
pipx install git+https://github.com/zexuyann/code-locate-cli.git
```

From source for development:

```bash
git clone git@github.com:zexuyann/code-locate-cli.git
cd code-locate-cli
uv sync
uv run code-locate --help
```

Verify:

```bash
code-locate --version
code-locate --help
```

Run locally without installing:

```bash
python -m code_locate.cli query --spec ../code-locate-skill/examples/search-plan.settings-persistence.json --repo /path/to/repo --top 5
```

## Requirements

- Python 3.10+
- `ripgrep` recommended for speed (`rg` on `PATH`)
- Python dependencies from `pyproject.toml`:
  - `tree-sitter`
  - `tree-sitter-language-pack`

If `rg` is unavailable, `code-locate` falls back to a Python filesystem scan with the same search-plan include/exclude rules.

## Usage

```bash
# Search with a structured plan
code-locate query --spec /tmp/search-plan.json --repo /path/to/repo --top 5 --json

# Raw query fallback
code-locate query "saveConfig localStorage settings" --repo /path/to/repo --top 5

# Read context around a result
code-locate context src/settings/saveConfig.ts:41 --repo /path/to/repo --radius 80 --symbol

# Expand dependency and caller-like signals
code-locate expand src/settings/saveConfig.ts:41 --repo /path/to/repo --depth 2 --top 20 --json

# Find grep-based references
code-locate refs saveConfig --repo /path/to/repo --top 20 --json
```

## Commands

### `query`

Ranks likely code locations.

```bash
code-locate query [query] [--spec search-plan.json] [--repo REPO] [--top N] [--json]
```

| Option | Default | Limit | Description |
| --- | ---: | ---: | --- |
| `--repo` | `.` | | Repository root |
| `--top` | `5` | `100` | Ranked result count |
| `--max-matches-per-term` | `200` | `2000` | Maximum collected matches per term |
| `--parse-limit` | `50` | `500` | Candidate files parsed for symbols |
| `--json` | off | | Emit machine-readable JSON |

### `context`

Shows source context around a location.

```bash
code-locate context path/to/file.py:123 [--repo REPO] [--radius N] [--symbol] [--json]
```

| Option | Default | Limit | Description |
| --- | ---: | ---: | --- |
| `--repo` | `.` | | Repository root |
| `--radius` | `40` | `500` | Lines before and after the target line |
| `--symbol` | off | | Expand to the enclosing symbol when possible |
| `--json` | off | | Emit machine-readable JSON |

The target file must stay inside `--repo`, must be a file, and must not exceed the per-file read limit.

### `refs`

Finds grep-based references for an identifier or phrase.

```bash
code-locate refs saveConfig [--repo REPO] [--top N] [--json]
```

`refs` is not language-server reference analysis. It is a bounded retrieval signal for agent follow-up.

### `expand`

Expands a file or symbol into nearby dependency signals.

```bash
code-locate expand path/to/file.py[:line[:column]] [--repo REPO] [--scope auto|symbol|file] [--depth N] [--top N] [--json]
```

It reports:

- imports found in the current file, using tree-sitter when available
- local dependencies resolved from relative Python, JavaScript/TypeScript, and C/C++ includes
- dependents that import the current file
- outgoing call sites in the current symbol or file
- local callees resolved to symbols in the same file
- imported callees tied back to import statements, including Python `super()` calls through imported base classes
- incoming call-like references to the current symbol
- related test/spec files by path
- a graph of nodes and edges for agent-driven follow-up

`--depth` expands the import graph up to three hops. Edges are syntax-static/tree-sitter or regex signals, not precise language-server analysis.

## Search Plan

Use structured search plans for agent workflows:

```json
{
  "issue": "点击保存后刷新页面配置丢失",
  "exact_phrases": ["保存", "配置"],
  "identifiers": ["saveConfig", "saveSettings", "loadSettings"],
  "concept_terms": ["save", "config", "settings", "persist", "reload"],
  "api_terms": ["/settings", "/config"],
  "storage_terms": ["localStorage", "sessionStorage", "indexedDB"],
  "framework_terms": ["useEffect", "loader"],
  "include_globs": ["src/**/*.ts", "src/**/*.tsx"],
  "exclude_globs": ["coverage/**"]
}
```

All fields are optional. Values must be lists of strings except `issue`, which is a string.

| Field | Use for |
| --- | --- |
| `exact_phrases` | UI labels, exact error messages, config keys, option names |
| `identifiers` | Functions, methods, classes, modules, variables, tests |
| `concept_terms` | Domain words and behavior terms |
| `api_terms` | Route fragments, RPC names, query keys, public APIs |
| `storage_terms` | Storage APIs, table names, persistence words |
| `framework_terms` | Hooks, loaders, actions, decorators, lifecycle names |
| `include_globs` | Restricting search to likely subsystems |
| `exclude_globs` | Additional excludes appended to built-in defaults |

Built-in excludes cover common generated, vendored, virtualenv, cache, coverage, `.env*`, and private-key paths.

## Structured Output

`--json` writes command output to stdout.

Example `query` result:

```json
{
  "query": "settings lost after refresh",
  "top": 5,
  "terms": [
    {"value": "saveConfig", "category": "identifier", "weight": 50}
  ],
  "match_count": 12,
  "result_count": 1,
  "results": [
    {
      "rank": 1,
      "path": "src/settings.ts",
      "lines": {"start": 10, "end": 24},
      "symbol": {"name": "saveConfig", "kind": "function", "start_line": 10, "end_line": 24},
      "score": 185,
      "confidence": "high",
      "matched_terms": ["saveConfig", "settings"],
      "evidence": [
        {"line": 10, "column": 17, "term": "saveConfig", "category": "identifier", "text": "export function saveConfig(...) {"}
      ],
      "suggested_next_steps": [
        {
          "argv": ["code-locate", "context", "src/settings.ts:10", "--radius", "80"],
          "display": "code-locate context src/settings.ts:10 --radius 80"
        }
      ]
    }
  ]
}
```

Agents should execute `suggested_next_steps[].argv`, not parse `display`.

When `--json` is present and an argument or runtime error occurs, the CLI writes a JSON error object to stderr and exits non-zero:

```json
{"error": {"type": "ValueError", "message": "query requires either a raw query or --spec"}}
```

## Safety And Scope

`code-locate` is intended to inspect local source repositories, not arbitrary filesystem roots.

- `context` and `expand` reject targets that resolve outside `--repo`
- `query` and `refs` require `--repo` to be an existing directory
- hidden files and directories are not searched by default
- hidden search is enabled only when `include_globs` explicitly names a hidden path segment, such as `.github/**`
- Python fallback skips files whose resolved path is outside `--repo`, including symlinks to external files
- files larger than 2 MB are skipped or rejected depending on command
- `.env*`, common private-key files, virtualenvs, caches, build output, vendored code, and generated output are excluded by default

Scores and confidence labels are prioritization hints, not proof of relevance or root cause.

## Use As AI Agent Skill

`code-locate-cli` is designed to work with the companion AI agent skill:

- CLI repository: [code-locate-cli](https://github.com/zexuyann/code-locate-cli)
- Skill repository: [code-locate-skill](https://github.com/zexuyann/code-locate-skill)

The skill is a `SKILL.md`-based guide that can be installed into Codex, Claude Code, or other tools that support local agent skills. It teaches agents how to:

- rewrite natural-language issues into search plans
- run `code-locate query --spec ... --json`
- inspect evidence and follow `context`, `expand`, and `refs`
- make root-cause judgments only after reading source code

After installing the skill, trigger it by mentioning `code-locate` in the agent request:

```text
Use code-locate to find the code related to this bug: settings disappear after refresh.
```

Generic manual skill install:

```bash
mkdir -p /path/to/agent-skills/code-locate
cp -R /path/to/code-locate-skill/. /path/to/agent-skills/code-locate/
```

For tools with their own skill directory, replace `/path/to/agent-skills` with that tool's configured location.

## Project Structure

```text
code_locate/
├── __init__.py
├── cli.py          # argparse entry point and command formatting
├── commands.py     # structured next-step command helpers
├── search_plan.py  # search-plan parsing, defaults, term weighting
├── search.py       # ripgrep/Python retrieval and match extraction
├── ranking.py      # candidate grouping and ranking
├── symbols.py      # tree-sitter and heuristic symbol parsing
├── expand.py       # dependency, reference, call, and graph expansion
└── models.py       # dataclasses for terms, matches, symbols, candidates
```

## Development

```bash
# Install dependencies
uv sync

# Run locally
uv run code-locate --help

# Run tests
uv run --with pytest pytest -q -p no:cacheprovider

# Strict resource-warning check
PYTHONDONTWRITEBYTECODE=1 uv run --with pytest python -W error::ResourceWarning -m pytest -q -p no:cacheprovider

# Standard-library fallback
python -m unittest discover -q
```

## Troubleshooting

**Q: `code-locate` command not found**

Install the CLI with `uv tool`:

```bash
uv tool install git+https://github.com/zexuyann/code-locate-cli.git
```

**Q: query returns no results**

Start with exact identifiers and phrases from the issue. If the subsystem is known, add `include_globs`. If the first plan is too narrow, add likely synonyms and API/storage/framework terms.

**Q: top results are only tests or docs**

Inspect the top test or doc briefly, extract production identifiers, then rerun `query` with tighter `include_globs`.

**Q: hidden files are missing**

Hidden files are skipped by default. Add explicit hidden `include_globs`, such as `.github/**`, only when those files are relevant.

**Q: suggested commands contain both `argv` and `display`**

Use `argv` for execution. `display` is a shell-escaped human-readable string for logs.

## License

MIT

---

## 功能特性

- **结构化检索** - 使用 `code-locate query --spec ...` 执行 issue search plan
- **grep 优先** - 优先使用 `ripgrep`，不可用时降级到 Python 扫描
- **符号聚合** - 将命中行聚合到函数、方法、类等代码范围
- **上下文阅读** - 用 `context` 查看 `path:line` 附近源码，可扩展到 enclosing symbol
- **依赖扩展** - 用 `expand` 查看 imports、dependencies、dependents、callees、incoming references 和相关测试
- **引用检索** - 用 `refs` 做 grep-based caller-like 探索
- **结构化 JSON** - 输出 evidence、score、confidence 和可执行 `argv`
- **安全默认值** - 默认跳过隐藏文件，并排除生成物、vendor、cache、`.env*` 和私钥路径
- **有界执行** - 对 `--top`、`--radius`、`--depth`、match 数和 parse 数设置上限
- **CLI 内不调用 LLM** - query rewrite 和根因判断由 agent / skill 完成

## 安装

```bash
git clone git@github.com:zexuyann/code-locate-cli.git
cd code-locate-cli
uv tool install .
code-locate --version
```

## 使用示例

```bash
# 使用结构化 search plan
code-locate query --spec /tmp/search-plan.json --repo /path/to/repo --top 5 --json

# 查看上下文
code-locate context src/settings/saveConfig.ts:41 --repo /path/to/repo --radius 80 --symbol

# 扩展依赖和引用信号
code-locate expand src/settings/saveConfig.ts:41 --repo /path/to/repo --depth 2 --top 20 --json

# 查找引用
code-locate refs saveConfig --repo /path/to/repo --top 20 --json
```

## 作为 AI Agent Skill 使用

推荐搭配 companion skill 使用。该 skill 基于 `SKILL.md`，可安装到 Codex、Claude Code 或其他支持本地 agent skill 的工具中：

- CLI 仓库：[code-locate-cli](https://github.com/zexuyann/code-locate-cli)
- Skill 仓库：[code-locate-skill](https://github.com/zexuyann/code-locate-skill)

skill 会指导 agent 将自然语言 issue 改写成 search plan，调用 CLI，阅读 evidence，并在阅读真实源码后再判断根因。

安装 skill 后，可以在请求里直接提到 `code-locate` 来触发：

```text
用 code-locate 帮我定位这个 bug 相关代码：点击保存后刷新页面配置丢失
```

## 结构化输出

`--json` 输出面向机器消费。`suggested_next_steps` 中的 `argv` 用于执行，`display` 只用于日志和复制粘贴。

## 许可证

MIT
