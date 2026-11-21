#!/usr/bin/env python3
"""
Daily AI Repo Audit -> GitHub Issues.

Runs once per day (scheduled in the caller repo). Makes ONE LLM call and files a
GitHub issue for each concrete, fixable finding. Issues are labeled so downstream
issue-triage or agent automations can pick them up.

Free-tier & incremental friendly:
- First run (or when `AUDIT_FULL=1`): full-repository baseline audit.
- Subsequent runs: incremental diff + 1-hop related-file audit since the last
  successful audited commit (tracked via runner cache state + issue markers).
- Skips the LLM call entirely if HEAD is unchanged or only ignored/non-code
  files changed since the last successful audit.
- Context is capped to a token budget; de-duplicates against existing open/closed issues.

Environment:
  GITHUB_TOKEN, GITHUB_REPOSITORY
  GEMINI_API_KEY | ANTHROPIC_API_KEY | OPENAI_API_KEY
  AI_REVIEW_MODEL (optional)
  AUDIT_LABEL (default: ai-audit), AUDIT_MAX_ISSUES (default: 5)
  AUDIT_FORCE (set to 1 to skip the already-audited-HEAD check)
  AUDIT_FULL (set to 1 to force a full-repository baseline audit)
  AUDIT_STATE_FILE (path to cached JSON state file tracking last_audited_sha)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_pr_reviewer as core  # noqa: E402

DEFAULT_MAX_ISSUES = 5
DEFAULT_AUDIT_TOKEN_BUDGET = 200_000  # keep under free-tier TPM (Gemini flash ~250k)
DEFAULT_INCREMENTAL_TOKEN_BUDGET = 80_000
DEFAULT_MAX_RELATED_FILES = 25
MAX_NEIGHBOR_FILE_BYTES = 100_000

FINGERPRINT_RE = re.compile(r"<!-- ai-audit-fingerprint:([0-9a-f]{12}) -->")
HEAD_RE = re.compile(r"<!-- ai-audit-head:([0-9a-f]{7,40}) -->")
QUOTED_STR_RE = re.compile(r"""["']([^"'\n]{2,160})["']""")
PATH_TOKEN_RE = re.compile(r"""(?:res://)?([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)""")
PY_FROM_RE = re.compile(r"""^\s*from\s+(\.*[A-Za-z0-9_.]*)\s+import\s+([^\n#]+)""", re.MULTILINE)
PY_IMPORT_RE = re.compile(r"""^\s*import\s+([A-Za-z0-9_.,\s]+)""", re.MULTILINE)

MANIFEST_FILENAMES = {
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "project.godot",
    "wrangler.toml",
    "wrangler.jsonc",
    "build.gradle.kts",
    "settings.gradle.kts",
    "build.gradle",
    "Cargo.toml",
    "go.mod",
}

GENERIC_FILENAMES = {
    "index.ts",
    "index.tsx",
    "index.js",
    "index.jsx",
    "main.py",
    "main.ts",
    "main.kt",
    "app.py",
    "app.tsx",
    "__init__.py",
    "readme.md",
    "package.json",
}

GENERIC_STEMS = {
    "index",
    "main",
    "init",
    "__init__",
    "app",
    "mod",
    "lib",
    "utils",
    "util",
    "helpers",
    "helper",
    "types",
    "constants",
    "config",
    "common",
    "shared",
    "base",
    "core",
    "test",
    "tests",
    "readme",
}

ARCH_GUIDELINE_DOCS = {
    "AGENTS.md",
    "CLAUDE.md",
    "docs/ARCHITECTURE.md",
}

AUDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "category": {
                        "type": "string",
                        "enum": ["bug", "security", "performance", "architecture", "maintainability", "docs"],
                    },
                    "files": {"type": "array", "items": {"type": "string"}},
                    "problem": {"type": "string"},
                    "suggested_fix": {"type": "string"},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "title",
                    "severity",
                    "category",
                    "files",
                    "problem",
                    "suggested_fix",
                    "acceptance_criteria",
                ],
            },
        },
    },
    "required": ["summary", "issues"],
}


def gh_request(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> Any:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else None


def ensure_label(repo: str, token: str, label: str) -> None:
    try:
        gh_request(
            "POST",
            f"https://api.github.com/repos/{repo}/labels",
            token,
            {"name": label, "color": "5319e7", "description": "Filed by the daily AI repo audit"},
        )
    except urllib.error.HTTPError as exc:
        if exc.code != 422:  # 422 = already exists
            print(f"WARNING: could not create label '{label}': HTTP {exc.code}")


def fetch_existing_audit_issues(repo: str, token: str, label: str) -> list[dict[str, Any]]:
    """Open + closed issues carrying the audit label (PRs excluded). Most recent first."""
    q = urllib.parse.urlencode({"labels": label, "state": "all", "per_page": 100, "sort": "created"})
    items = gh_request("GET", f"https://api.github.com/repos/{repo}/issues?{q}", token) or []
    return [i for i in items if "pull_request" not in i]


def current_head() -> str:
    return core.run_git(["rev-parse", "HEAD"]).strip()


def make_fingerprint(title: str, files: list[str]) -> str:
    key = re.sub(r"\W+", " ", title.lower()).strip() + "|" + ",".join(sorted(files))
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def load_audit_state(state_path: Path | None) -> dict[str, Any]:
    """Load persisted audit state JSON safely; return {} if missing or invalid."""
    if state_path is None or not state_path.is_file():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_audit_state(state_path: Path | None, head: str, mode: str) -> None:
    """Persist the last successfully audited commit SHA for future incremental runs."""
    if state_path is None:
        return
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_audited_sha": head, "last_mode": mode}
    state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_github_output(name: str, value: str) -> None:
    """Write a key=value pair to GITHUB_OUTPUT when running inside GitHub Actions."""
    output_file = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not output_file:
        return
    with Path(output_file).open("a", encoding="utf-8") as fh:
        fh.write(f"{name}={value}\n")


def is_valid_ancestor(base_sha: str, head: str) -> bool:
    """Return True when base_sha resolves to a commit that is an ancestor of head."""
    if not base_sha or not head:
        return False
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{base_sha}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if exists.returncode != 0:
        return False
    anc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_sha, head],
        capture_output=True,
        text=True,
        check=False,
    )
    return anc.returncode == 0


def resolve_last_audited_sha(
    state: dict[str, Any],
    existing: list[dict[str, Any]],
    head: str,
) -> str | None:
    """Pick the freshest valid baseline SHA from cached state or filed audit issues."""
    state_sha = str(state.get("last_audited_sha", "")).strip() or None
    issue_sha: str | None = None
    for issue in existing:
        m = HEAD_RE.search(issue.get("body") or "")
        if m:
            issue_sha = m.group(1)
            break

    valid_candidates = [sha for sha in (state_sha, issue_sha) if sha and is_valid_ancestor(sha, head)]
    if not valid_candidates:
        return state_sha or issue_sha
    if len(valid_candidates) == 1:
        return valid_candidates[0]

    assert state_sha is not None and issue_sha is not None
    return issue_sha if is_valid_ancestor(state_sha, issue_sha) else state_sha


def get_repo_filter_sets(
    config: dict[str, Any],
) -> tuple[set[str], set[str], tuple[str, ...], set[str]]:
    ignored_dirs = set(core.DEFAULT_IGNORED_DIRS) | set(config.get("ignored_dirs", []))
    ignored_files = set(core.DEFAULT_IGNORED_FILES) | set(config.get("ignored_files", []))
    ignored_prefixes = tuple(
        list(core.DEFAULT_IGNORED_PATH_PREFIXES) + list(config.get("ignored_path_prefixes", []))
    )
    code_extensions = (
        set(config["code_extensions"])
        if "code_extensions" in config
        else set(core.DEFAULT_CODE_EXTENSIONS)
    )
    return ignored_dirs, ignored_files, ignored_prefixes, code_extensions


def is_code_file(rel_path: Path, code_extensions: set[str]) -> bool:
    return rel_path.suffix in code_extensions or rel_path.name in core.SPECIAL_INCLUDED_FILENAMES


def get_changes_since(
    base_sha: str,
    head: str,
    config: dict[str, Any],
) -> tuple[list[tuple[str, str, str | None]], list[str]]:
    """Return non-ignored (status, path, old_path) changes and relevant code paths."""
    ignored_dirs, ignored_files, ignored_prefixes, code_extensions = get_repo_filter_sets(config)
    raw = core.run_git(
        ["-c", "core.quotepath=false", "diff", "--name-status", "--find-renames", f"{base_sha}..{head}"]
    )

    changes: list[tuple[str, str, str | None]] = []
    relevant: set[str] = set()
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0].strip()
        if status.startswith(("R", "C")) and len(parts) >= 3:
            old_path, new_path = parts[1], parts[2]
            old_ignored = core.should_ignore(Path(old_path), ignored_dirs, ignored_files, ignored_prefixes)
            new_ignored = core.should_ignore(Path(new_path), ignored_dirs, ignored_files, ignored_prefixes)
            if old_ignored and new_ignored:
                continue
            changes.append((status[0], new_path, old_path))
            if not new_ignored and is_code_file(Path(new_path), code_extensions):
                relevant.add(new_path)
            if not old_ignored and is_code_file(Path(old_path), code_extensions):
                relevant.add(old_path)
        elif len(parts) >= 2:
            path_str = parts[1]
            if core.should_ignore(Path(path_str), ignored_dirs, ignored_files, ignored_prefixes):
                continue
            changes.append((status[0], path_str, None))
            if is_code_file(Path(path_str), code_extensions):
                relevant.add(path_str)

    return changes, sorted(relevant)


def get_commit_log(base_sha: str, head: str) -> str:
    return core.run_git(["log", "--oneline", "--max-count=50", f"{base_sha}..{head}"]).strip()


def get_filtered_diff(base_sha: str, head: str, paths: list[str]) -> str:
    if not paths:
        return ""
    chunks: list[str] = []
    for i in range(0, len(paths), 50):
        batch = paths[i : i + 50]
        chunk = core.run_git(["diff", "--unified=3", f"{base_sha}..{head}", "--", *batch]).strip()
        if chunk:
            chunks.append(chunk)
    return "\n\n".join(chunks)


def _list_non_ignored_repo_files(
    repo_root: Path,
    ignored_dirs: set[str],
    ignored_files: set[str],
    ignored_prefixes: tuple[str, ...],
) -> list[tuple[Path, Path, str]]:
    git_files_out = core.run_git(["ls-files", "--cached", "--others", "--exclude-standard"])
    if git_files_out.strip():
        all_paths = [repo_root / line.strip() for line in git_files_out.splitlines() if line.strip()]
    else:
        all_paths = sorted(repo_root.rglob("*"))

    results: list[tuple[Path, Path, str]] = []
    for path in all_paths:
        if not path.is_file():
            continue
        rel = path.relative_to(repo_root)
        if core.should_ignore(rel, ignored_dirs, ignored_files, ignored_prefixes):
            continue
        results.append((path, rel, rel.as_posix()))
    return results


def _normalize_hint_path(raw: str) -> str | None:
    cleaned = raw.strip()
    if not cleaned or ("://" in cleaned and not cleaned.startswith("res://")):
        return None
    if cleaned.startswith("res://"):
        cleaned = cleaned[len("res://") :]
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    while cleaned.startswith("../"):
        cleaned = cleaned[3:]
    cleaned = cleaned.strip("/")
    return cleaned or None


def _extract_outgoing_hints(changed_contents: dict[str, str]) -> tuple[set[str], set[str]]:
    path_hints: set[str] = set()
    stem_hints: set[str] = set()

    for content in changed_contents.values():
        for match in QUOTED_STR_RE.finditer(content):
            candidate = match.group(1).strip()
            if "/" in candidate or "." in candidate:
                norm = _normalize_hint_path(candidate)
                if norm:
                    path_hints.add(norm)
        for match in PATH_TOKEN_RE.finditer(content):
            norm = _normalize_hint_path(match.group(1))
            if norm:
                path_hints.add(norm)
        for match in PY_FROM_RE.finditer(content):
            mod = match.group(1).lstrip(".")
            imported = match.group(2)
            slash = mod.replace(".", "/") if mod else ""
            if slash:
                path_hints.add(slash)
                last = mod.split(".")[-1]
                if len(last) >= 4 and last.lower() not in GENERIC_STEMS:
                    stem_hints.add(last)
            for part in imported.strip("() ").split(","):
                sym = part.strip().split(" as ")[0].strip()
                if not sym or sym == "*":
                    continue
                if slash:
                    path_hints.add(f"{slash}/{sym}")
                elif len(sym) >= 4 and sym.lower() not in GENERIC_STEMS:
                    stem_hints.add(sym)
        for match in PY_IMPORT_RE.finditer(content):
            for part in match.group(1).split(","):
                mod = part.strip().split(" as ")[0].strip()
                if mod:
                    path_hints.add(mod.replace(".", "/"))
                    last = mod.split(".")[-1]
                    if len(last) >= 4 and last.lower() not in GENERIC_STEMS:
                        stem_hints.add(last)

    return path_hints, stem_hints


def _matches_outgoing_hint(rel_str: str, path_hints: set[str], stem_hints: set[str]) -> bool:
    rel_no_ext = Path(rel_str).with_suffix("").as_posix()
    rel_stem = Path(rel_str).stem
    if rel_stem in stem_hints:
        return True
    for hint in path_hints:
        if (
            rel_str == hint
            or rel_str.endswith("/" + hint)
            or rel_no_ext == hint
            or rel_no_ext.endswith("/" + hint)
            or rel_no_ext == f"{hint}/index"
            or rel_no_ext == f"{hint}/__init__"
            or rel_no_ext.endswith(f"/{hint}/index")
            or rel_no_ext.endswith(f"/{hint}/__init__")
        ):
            return True
    return False


def _format_changes_summary(changes: list[tuple[str, str, str | None]]) -> str:
    lines: list[str] = []
    for status, path_str, old_path in changes:
        if old_path:
            lines.append(f"- `{status}`: `{old_path}` -> `{path_str}`")
        else:
            lines.append(f"- `{status}`: `{path_str}`")
    return "\n".join(lines) or "- (none)"


def _read_display_text(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    return core.compact_tscn(raw) if path.suffix == ".tscn" else raw


def collect_incremental_context(
    repo_root: Path,
    base_sha: str,
    head: str,
    changes: list[tuple[str, str, str | None]],
    relevant_paths: list[str],
    diff_text: str,
    commit_log: str,
    config: dict[str, Any],
    budget_tokens: int,
) -> tuple[str, dict[str, int]]:
    """Build diff-first context with changed files and 1-hop related neighbors."""
    ignored_dirs, ignored_files, ignored_prefixes, code_extensions = get_repo_filter_sets(config)
    max_related = int(config.get("audit_max_related_files", DEFAULT_MAX_RELATED_FILES))
    repo_files = _list_non_ignored_repo_files(repo_root, ignored_dirs, ignored_files, ignored_prefixes)

    tree_lines: list[str] = []
    code_files: list[tuple[Path, Path, str, int]] = []
    total_code_bytes = 0
    for path, rel, rel_str in repo_files:
        tree_lines.append(f"- {rel_str}")
        if is_code_file(rel, code_extensions):
            size = path.stat().st_size
            total_code_bytes += size
            code_files.append((path, rel, rel_str, size))

    full_repo_est_tokens = max(1, total_code_bytes // 4)
    changed_non_deleted = {
        path_str
        for status, path_str, _ in changes
        if status != "D" and is_code_file(Path(path_str), code_extensions)
    }

    changed_contents: dict[str, str] = {}
    for rel_str in sorted(changed_non_deleted):
        full_path = repo_root / rel_str
        if full_path.is_file():
            changed_contents[rel_str] = _read_display_text(full_path)

    path_hints, stem_hints = _extract_outgoing_hints(changed_contents)
    exact_paths = set(relevant_paths)
    specific_filenames = {
        Path(p).name for p in relevant_paths if Path(p).name.lower() not in GENERIC_FILENAMES
    }
    stem_patterns = [
        re.compile(rf"\b{re.escape(Path(p).stem)}\b")
        for p in relevant_paths
        if len(Path(p).stem) >= 4 and Path(p).stem.lower() not in GENERIC_STEMS
    ]

    neighbors: list[tuple[int, int, str, str]] = []
    for path, rel, rel_str, size in code_files:
        if rel_str in changed_non_deleted or rel_str in ARCH_GUIDELINE_DOCS:
            continue
        if size > MAX_NEIGHBOR_FILE_BYTES:
            continue

        is_root_manifest = rel.parent == Path(".") and rel.name in MANIFEST_FILENAMES
        outgoing_hit = _matches_outgoing_hint(rel_str, path_hints, stem_hints)
        text = _read_display_text(path)

        tier: int | None = None
        if outgoing_hit or any(ep in text for ep in exact_paths) or any(fn in text for fn in specific_filenames):
            tier = 1
        elif any(pat.search(text) for pat in stem_patterns):
            tier = 2
        elif is_root_manifest:
            tier = 3

        if tier is not None:
            neighbors.append((tier, size, rel_str, text))

    neighbors.sort(key=lambda item: (item[0], item[1], item[2]))
    selected_neighbors = neighbors[:max_related]

    budget_chars = max(4_000, budget_tokens * 4)
    sections: list[str] = [
        f"## Commits Since Last Successful Audit ({base_sha[:7]}..{head[:7]})\n{commit_log or '(no commit messages)'}",
        f"## Changed Files Summary\n{_format_changes_summary(changes)}",
    ]
    used_chars = sum(len(s) for s in sections)

    tree_cap = max(2_000, int(budget_chars * 0.20))
    tree_body = "\n".join(tree_lines)
    if len(tree_body) > tree_cap:
        tree_body = tree_body[:tree_cap].rsplit("\n", 1)[0] + "\n... [tree truncated]"
    tree_section = f"## Repository File Tree\n{tree_body}"
    sections.append(tree_section)
    used_chars += len(tree_section)

    diff_cap = max(4_000, int(budget_chars * 0.45))
    trimmed_diff = diff_text
    if len(trimmed_diff) > diff_cap:
        trimmed_diff = trimmed_diff[:diff_cap] + "\n... [diff truncated to stay within incremental token budget]"
    diff_section = f"## Diff Since Last Successful Audit\n```diff\n{trimmed_diff}\n```"
    sections.append(diff_section)
    used_chars += len(diff_section)

    changed_blocks: list[str] = []
    included_changed = 0
    for rel_str, content in changed_contents.items():
        remaining = budget_chars - used_chars
        if remaining < 500:
            break
        block = f"### File: `{rel_str}`\n```\n{content}\n```"
        if len(block) > remaining:
            if remaining < 1_000:
                break
            excerpt = content[: remaining - 200] + "\n... [file truncated]"
            block = f"### File: `{rel_str}`\n```\n{excerpt}\n```"
        changed_blocks.append(block)
        used_chars += len(block)
        included_changed += 1
    if changed_blocks:
        sections.append("## Changed / Added Files (Full Context)\n\n" + "\n\n".join(changed_blocks))

    related_blocks: list[str] = []
    included_related = 0
    for _, _, rel_str, content in selected_neighbors:
        remaining = budget_chars - used_chars
        if remaining < 500:
            break
        block = f"### Related File: `{rel_str}`\n```\n{content}\n```"
        if len(block) > remaining:
            continue
        related_blocks.append(block)
        used_chars += len(block)
        included_related += 1
    if related_blocks:
        sections.append("## Related / Potentially Impacted Files (1-Hop Context)\n\n" + "\n\n".join(related_blocks))

    context_blob = "\n\n".join(sections)
    stats = {
        "est_tokens": max(1, len(context_blob) // 4),
        "full_repo_est_tokens": full_repo_est_tokens,
        "changed_files": included_changed,
        "related_files": included_related,
    }
    return context_blob, stats


def build_audit_prompt(
    repo_root: Path,
    repo_name: str,
    config: dict[str, Any],
    max_issues: int,
    base_sha: str | None = None,
    head: str = "",
) -> str:
    project_label = config.get("project_name") or repo_name or repo_root.name
    stacks = core.detect_stack_summary(repo_root)
    stack_str = ", ".join(stacks) if stacks else "Software Engineering & Architecture"
    guidelines = core.load_architecture_guidelines(repo_root)
    extra = config.get("extra_instructions", [])
    extra_block = ("\n## Extra Instructions\n" + "\n".join(f"- {e}" for e in extra) + "\n") if extra else ""
    guidelines_block = (
        f"\n## Repository Rules (authoritative; flag violations)\n{guidelines}\n" if guidelines else ""
    )

    if base_sha:
        scope_intro = (
            f'You are a Senior Principal Engineer auditing incremental changes (`{base_sha[:7]}..{head[:7]}`) '
            f'in repository "{project_label}" ({stack_str}).\n'
            "Find concrete, high-confidence, FIXABLE problems introduced by, exposed by, or directly impacted by "
            "these new changes (including ripple effects on related files, configs, workflows, and repository rules). "
            "Report each as a self-contained work item that an autonomous coding agent can pick up and fix without further context."
        )
        scope_rule = (
            "- Focus strictly on the new changes and their impact on related files; "
            "do NOT report unrelated pre-existing issues in untouched code.\n"
        )
    else:
        scope_intro = (
            f'You are a Senior Principal Engineer auditing the whole repository "{project_label}" ({stack_str}).\n'
            "Find concrete, high-confidence, FIXABLE problems and report each as a self-contained work item that an "
            "autonomous coding agent can pick up and fix without further context."
        )
        scope_rule = ""

    return f"""{scope_intro}

Look for: real bugs, broken references/imports/paths, security/privacy issues (leaked secrets, unsafe input),
performance/resource problems, violations of the repository's documented architecture rules, and missing
validation or error handling.

Rules:
- Report at most {max_issues} issues, highest severity first. Fewer is better; return an empty list if the repo is clean.
{scope_rule}- Each issue must reference real files that exist in the provided context, with enough detail (function names,
  line hints) to locate the problem. Never speculate or pad with style nitpicks.
- Each issue must be independently fixable in a single small PR.
- Do NOT report issues that match any "Already Tracked" item provided by the user.
- Today's environment is 2026: Godot 4.7, React 19, Vite 6, Tailwind v4, wrangler v4, Kotlin 2.x,
  actions/checkout@v5, actions/setup-python@v6, actions/cache@v5, Google GenAI SDK `client.interactions.create`,
  `gemini-3.8-flash` and `gemini-4-argon` are valid and current. NEVER flag them as non-existent.
{extra_block}{guidelines_block}
Respond ONLY with JSON matching: {{"summary": str, "issues": [{{"title","severity","category","files","problem","suggested_fix","acceptance_criteria"}}]}}
"""


def render_issue_body(
    item: dict[str, Any],
    fingerprint: str,
    head: str,
    model_label: str,
    base_sha: str | None = None,
) -> str:
    files = "\n".join(f"- `{f}`" for f in item.get("files", [])) or "- (unspecified)"
    criteria = "\n".join(f"- [ ] {c}" for c in item.get("acceptance_criteria", [])) or "- [ ] Issue resolved"
    scope_note = f"incremental `{base_sha[:7]}..{head[:7]}`" if base_sha else f"full baseline at `{head[:7]}`"
    base_marker = f"\n<!-- ai-audit-base:{base_sha} -->" if base_sha else ""
    return (
        f"**Severity:** `{item['severity']}` | **Category:** `{item['category']}`\n\n"
        f"## Problem\n{item['problem'].strip()}\n\n"
        f"## Affected files\n{files}\n\n"
        f"## Suggested fix\n{item['suggested_fix'].strip()}\n\n"
        f"## Acceptance criteria\n{criteria}\n\n"
        f"---\n_Filed automatically by the daily AI repo audit ({model_label}, {scope_note})._\n"
        f"<!-- ai-audit-fingerprint:{fingerprint} -->\n"
        f"<!-- ai-audit-head:{head} -->{base_marker}"
    )


def call_provider(system_prompt: str, user_prompt: str) -> tuple[dict[str, Any], str] | None:
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    custom_model = os.environ.get("AI_REVIEW_MODEL", "").strip()

    # Prefer Gemini first here: the whole point is staying on its free tier.
    if gemini_key:
        model = custom_model or "gemini-3.8-flash"
        return core.call_gemini(gemini_key, model, system_prompt, user_prompt, schema=AUDIT_SCHEMA), f"Gemini `{model}`"
    if anthropic_key:
        model = custom_model or "claude-sonnet-4-5"
        return core.call_anthropic(anthropic_key, model, system_prompt, user_prompt), f"Claude `{model}`"
    if openai_key:
        model = custom_model or "gpt-4o"
        return core.call_openai(openai_key, model, system_prompt, user_prompt), f"OpenAI `{model}`"
    return None


def main() -> int:
    repo_root = Path.cwd()
    config = core.load_repo_config(repo_root)
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    label = os.environ.get("AUDIT_LABEL", "ai-audit").strip() or "ai-audit"
    max_issues_raw = os.environ.get("AUDIT_MAX_ISSUES", "").strip()
    max_issues = int(max_issues_raw or config.get("audit_max_issues", DEFAULT_MAX_ISSUES))
    force = os.environ.get("AUDIT_FORCE", "").strip() in ("1", "true")
    full_audit = os.environ.get("AUDIT_FULL", "").strip() in ("1", "true")
    state_file_env = os.environ.get("AUDIT_STATE_FILE", "").strip()
    state_path = Path(state_file_env) if state_file_env else None

    if not (repo and token):
        print("ERROR: GITHUB_REPOSITORY and GITHUB_TOKEN are required.")
        return 1
    if not any(os.environ.get(k, "").strip() for k in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")):
        print("SKIP: No AI API key configured.")
        return 0

    head = current_head()
    existing = fetch_existing_audit_issues(repo, token, label)
    state = load_audit_state(state_path)
    last_sha = resolve_last_audited_sha(state, existing, head)

    if not force and not full_audit and last_sha == head:
        print(f"SKIP: HEAD {head[:7]} already audited. Set AUDIT_FORCE=1 or full_audit=true to override.")
        return 0

    is_incremental = (
        not full_audit
        and bool(last_sha)
        and last_sha != head
        and is_valid_ancestor(last_sha, head)
    )

    full_budget = int(config.get("audit_token_budget", DEFAULT_AUDIT_TOKEN_BUDGET))
    mode_base_sha: str | None = None

    if is_incremental:
        assert last_sha is not None
        print(f"Audit mode: incremental ({last_sha[:7]}..{head[:7]}).")
        changes, relevant_paths = get_changes_since(last_sha, head, config)
        diff_text = get_filtered_diff(last_sha, head, relevant_paths)

        if not relevant_paths and not diff_text.strip():
            print(
                f"SKIP: Only ignored or non-code files changed since {last_sha[:7]}; "
                f"advancing audit state to {head[:7]}."
            )
            save_audit_state(state_path, head, mode="incremental-noop")
            write_github_output("state_updated", "true")
            return 0

        inc_budget = min(
            full_budget,
            int(config.get("audit_incremental_token_budget", DEFAULT_INCREMENTAL_TOKEN_BUDGET)),
        )
        commit_log = get_commit_log(last_sha, head)
        context_blob, inc_stats = collect_incremental_context(
            repo_root=repo_root,
            base_sha=last_sha,
            head=head,
            changes=changes,
            relevant_paths=relevant_paths,
            diff_text=diff_text,
            commit_log=commit_log,
            config=config,
            budget_tokens=inc_budget,
        )
        full_est = inc_stats["full_repo_est_tokens"]
        inc_est = inc_stats["est_tokens"]
        pct_saved = max(0.0, (1.0 - (inc_est / full_est)) * 100.0) if full_est else 0.0
        print(
            f"Incremental context: ~{inc_est:,} tokens vs ~{full_est:,} full repo "
            f"({pct_saved:.1f}% reduction; {inc_stats['changed_files']} changed, "
            f"{inc_stats['related_files']} related files)."
        )
        mode_base_sha = last_sha
        context_heading = "# Incremental Changes & Related Context"
    else:
        if full_audit:
            reason = "AUDIT_FULL=1 requested"
        elif force and last_sha == head:
            reason = f"AUDIT_FORCE=1 on already-audited HEAD {head[:7]}"
        elif last_sha and not is_valid_ancestor(last_sha, head):
            reason = f"previous baseline {last_sha[:7]} is not an ancestor of {head[:7]} (shallow or rewritten history)"
        else:
            reason = "no prior audit baseline found (first run)"
        print(f"Audit mode: full baseline ({reason}).")
        context_blob, metrics = core.collect_repo_context(repo_root, config)
        if metrics["est_tokens"] > full_budget:
            print(f"WARNING: context ~{metrics['est_tokens']:,} tokens exceeds budget {full_budget:,}; truncating.")
            context_blob = context_blob[: full_budget * 4]
        else:
            print(f"Full baseline context: ~{metrics['est_tokens']:,} tokens.")
        context_heading = "# Full Repository Context"

    tracked_lines = [
        f"- [{i['state']}] #{i['number']}: {i['title']}" for i in existing[:100]
    ]
    tracked = "\n".join(tracked_lines) or "(none)"
    user_prompt = (
        f"# Already Tracked (do not duplicate)\n{tracked}\n\n"
        f"{context_heading}\n{context_blob}\n"
    )
    system_prompt = build_audit_prompt(
        repo_root,
        repo,
        config,
        max_issues,
        base_sha=mode_base_sha,
        head=head,
    )

    try:
        result = call_provider(system_prompt, user_prompt)
    except urllib.error.HTTPError as exc:
        print(f"ERROR calling AI provider ({exc.code}): {exc.read().decode('utf-8', errors='ignore')}")
        return 1
    if result is None:
        return 0
    data, model_label = result

    print(f"Audit summary: {data.get('summary', '')}")
    known = {m.group(1) for i in existing if (m := FINGERPRINT_RE.search(i.get("body") or ""))}
    known_titles = {re.sub(r"\W+", " ", i["title"].lower()).strip() for i in existing}

    ensure_label(repo, token, label)
    created = 0
    for item in (data.get("issues") or [])[:max_issues]:
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        fp = make_fingerprint(title, item.get("files", []))
        if fp in known or re.sub(r"\W+", " ", title.lower()).strip() in known_titles:
            print(f"Skip duplicate: {title}")
            continue
        labels = [label, f"severity:{item.get('severity', 'low')}"]
        # severity label may not exist; GitHub auto-creates unknown labels on issue creation.
        issue = gh_request(
            "POST",
            f"https://api.github.com/repos/{repo}/issues",
            token,
            {
                "title": f"[AI Audit] {title}"[:250],
                "body": render_issue_body(item, fp, head, model_label, base_sha=mode_base_sha),
                "labels": labels,
            },
        )
        created += 1
        print(f"Created issue #{issue['number']}: {title}")

    if created == 0:
        print("No new issues filed.")

    save_audit_state(state_path, head, mode="incremental" if is_incremental else "full")
    write_github_output("state_updated", "true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
