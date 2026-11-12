#!/usr/bin/env python3
"""
Universal Holistic Repo-Aware AI Pull Request Reviewer.

Centralized in `jameslac/github-workflows` and invoked via reusable workflow across
personal and organization repositories.

Features:
- Zero-config auto-detection of repository stack (Godot 4, TypeScript/React/Cloudflare,
  Kotlin/Android/Jetpack Compose, Python, etc.).
- Dynamically reads each repository's `AGENTS.md`, `CLAUDE.md`, and `docs/ARCHITECTURE.md`
  to enforce repo-specific architecture and engineering rules without hardcoding them.
- Supports optional `.ai-review.json` in any repository for custom overrides (extensions,
  ignored paths, extra prompt instructions, or budget limits).
- Automatically compacts Godot `.tscn` scene files and excludes noisy lockfiles.
- Tracks repository scale (lines & estimated tokens) against the single-pass budget
  and warns on the PR when approaching the threshold (~75%).
- Supports Anthropic (ANTHROPIC_API_KEY), OpenAI (OPENAI_API_KEY), or Gemini (GEMINI_API_KEY)
  with automatic retry on transient 429/503 errors.
- Posts both a holistic PR summary review AND line-specific inline comments via the
  GitHub Pull Request Reviews REST API.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

# Default Thresholds for Option 1 (Single-Pass Whole-Repo Context)
DEFAULT_TOKEN_BUDGET_LIMIT = 80_000
DEFAULT_LINE_BUDGET_LIMIT = 80_000
DEFAULT_WARNING_RATIO = 0.75  # Warn at 75% capacity (~60k tokens / ~60k lines)

DEFAULT_IGNORED_DIRS = {
    ".git",
    ".godot",
    ".import",
    ".mono",
    "__pycache__",
    "node_modules",
    "dist",
    "dist-ssr",
    ".wrangler",
    "coverage",
    "build",
    ".gradle",
    ".idea",
    ".vscode",
    ".next",
    "target",
    "vendor",
    ".venv",
    "venv",
}

DEFAULT_IGNORED_PATH_PREFIXES = (
    "addons/godot_ai",
    "addons/.godot_ai_update",
)

DEFAULT_IGNORED_FILES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "poetry.lock",
    "Pipfile.lock",
    "gradle.lockfile",
}

DEFAULT_CODE_EXTENSIONS = {
    # Web / TypeScript / JavaScript
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".css",
    ".scss",
    ".html",
    ".vue",
    ".svelte",
    # Godot 4
    ".gd",
    ".tres",
    ".tscn",
    ".cfg",
    ".gdshader",
    # Kotlin / Java / Android
    ".kt",
    ".kts",
    ".java",
    ".xml",
    ".pro",
    ".toml",
    # Python / Systems / Other
    ".py",
    ".rs",
    ".go",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".sh",
    ".sql",
    ".graphql",
    ".proto",
    # Config & Docs
    ".json",
    ".jsonc",
    ".md",
    ".yml",
    ".yaml",
}

SPECIAL_INCLUDED_FILENAMES = {
    "project.godot",
    "Dockerfile",
    "Makefile",
    "Procfile",
    "Gemfile",
    "AndroidManifest.xml",
}


def load_repo_config(repo_root: Path) -> dict[str, Any]:
    """Load optional `.ai-review.json` overrides from the caller repository root."""
    config_path = repo_root / ".ai-review.json"
    if not config_path.is_file():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"WARNING: Failed to parse .ai-review.json ({exc}); falling back to defaults.")
        return {}


def detect_stack_summary(repo_root: Path) -> list[str]:
    """Auto-detect tech stack characteristics from repository files."""
    stacks: list[str] = []
    if (repo_root / "project.godot").is_file():
        stacks.append("Godot 4 Engine (GDScript, scenes `.tscn`, resources `.tres`)")
    if (repo_root / "wrangler.jsonc").is_file() or (repo_root / "wrangler.toml").is_file():
        stacks.append("Cloudflare Workers")
    if (repo_root / "package.json").is_file():
        try:
            pkg = json.loads((repo_root / "package.json").read_text(encoding="utf-8", errors="ignore"))
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            web_parts = ["TypeScript/JavaScript"]
            if "react" in deps:
                web_parts.append("React")
            if "vite" in deps:
                web_parts.append("Vite")
            if "tailwindcss" in deps or "@tailwindcss/vite" in deps:
                web_parts.append("Tailwind CSS")
            stacks.append(" + ".join(web_parts))
        except Exception:
            stacks.append("Node.js / TypeScript / JavaScript")
    if (
        (repo_root / "build.gradle.kts").is_file()
        or (repo_root / "settings.gradle.kts").is_file()
        or (repo_root / "build.gradle").is_file()
    ):
        stacks.append("Kotlin / Android / Gradle")
    if (repo_root / "pyproject.toml").is_file() or (repo_root / "requirements.txt").is_file():
        stacks.append("Python")
    return stacks


def load_architecture_guidelines(repo_root: Path) -> str:
    """Read AGENTS.md, CLAUDE.md, and docs/ARCHITECTURE.md if present in the repository."""
    doc_candidates = [
        "AGENTS.md",
        "CLAUDE.md",
        "docs/ARCHITECTURE.md",
    ]
    sections: list[str] = []
    for rel_doc in doc_candidates:
        doc_path = repo_root / rel_doc
        if doc_path.is_file():
            content = doc_path.read_text(encoding="utf-8", errors="ignore").strip()
            if content:
                sections.append(f"### `{rel_doc}`\n{content}")
    return "\n\n".join(sections)


def build_system_prompt(repo_root: Path, repo_name: str, config: dict[str, Any]) -> str:
    """Construct a tailored system prompt dynamically from repo docs, stack detection, and config."""
    project_label = config.get("project_name") or repo_name or repo_root.name
    stacks = detect_stack_summary(repo_root)
    stack_str = ", ".join(stacks) if stacks else "Software Engineering & Architecture"
    guidelines_blob = load_architecture_guidelines(repo_root)
    extra_instructions: list[str] = config.get("extra_instructions", [])

    extra_block = ""
    if extra_instructions:
        bullets = "\n".join(f"   - {item}" for item in extra_instructions)
        extra_block = f"\n5. Repository-Specific Custom Instructions (`.ai-review.json`):\n{bullets}\n"

    guidelines_section = ""
    if guidelines_blob:
        guidelines_section = (
            "\n## Repository Architecture & Agent Rules (Authoritative Source of Truth)\n"
            "The following guidelines come directly from this repository's documentation and MUST be enforced:\n\n"
            f"{guidelines_blob}\n"
        )

    return f"""You are a Senior Principal Engineer and Architecture Reviewer for "{project_label}" ({stack_str}).
You are reviewing a Pull Request with FULL holistic context of the repository.

Review the PR against:
1. Project Architecture & Rules (`AGENTS.md`, `docs/ARCHITECTURE.md`, `docs/API_REFERENCE.md`, etc.):
   - Enforce all architectural boundaries, dependency rules, privacy/credential handling rules, coding conventions, and CI/validator expectations documented in the repository.
   - If `AGENTS.md` specifies rules for architecture changes (such as deciding whether a new GitHub check/validator is needed under `.github/workflows/`), verify compliance.
   - In Godot repositories (`project.godot`), enforce modular scene/script boundaries (e.g., `minigames/<game_name>/` isolation vs `core/` and `shared/`), static typing in GDScript, and editor plugin hygiene (`addons/godot_ai` or `_mcp_game_helper` must never be committed).
2. Holistic Impact:
   - Explain how these changes fit into and impact existing modules, contracts, data models, APIs, UI components, workflows, or shared utilities across the repository.
3. Bugs, Performance & Security/Vulnerabilities:
   - Identify concrete bugs, broken references/imports/node paths, race conditions, memory/resource leaks, unhandled errors, security/privacy risks (e.g., leaked secrets/tokens, SSRF, open proxies, unsafe deserialization), or API rate-limit hazards.
4. Avoid Training-Cutoff False Positives:
   - Today's environment is 2026: Godot `4.7`, React `19`, Vite `6`, Tailwind CSS `v4` (`@tailwindcss/vite`), Cloudflare `wrangler` v4, Kotlin `2.x` / Jetpack Compose, GitHub Actions `actions/checkout@v5` and `actions/setup-python@v6`, and Google GenAI SDK `client.interactions.create` with `gemini-3.8-flash` or `gemini-4-argon` are all valid and current. NEVER flag these identifiers, action versions, or SDK methods as "non-existent".
   - Only report concrete, high-confidence issues. If the PR is clean and follows all project rules, return `"verdict": "APPROVE"`, `"risks_and_suggestions": []`, and `"inline_comments": []`.{extra_block}{guidelines_section}
Respond ONLY with a valid JSON object matching this exact schema:
{{
  "verdict": "APPROVE" | "COMMENT" | "NEEDS_ATTENTION",
  "headline": "One sentence overall takeaway.",
  "holistic_impact": "Markdown bullet list explaining how this PR fits into and impacts the broader repo.",
  "strengths": ["Markdown bullet 1", "Markdown bullet 2"],
  "risks_and_suggestions": ["Markdown bullet 1 (or empty list if none)"],
  "inline_comments": [
    {{
      "path": "relative/path/to/file.ext",
      "line": 42,
      "body": "Concise, constructive inline note or suggestion for this specific line in the PR diff."
    }}
  ]
}}

Rules for `inline_comments`:
- Only attach inline comments to lines that were added or modified in the PR diff (shown with `+` in the diff).
- Keep inline comments high-signal and actionable. If no inline comments are needed, return `"inline_comments": []`.
"""


def run_git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def should_ignore(
    rel_path: Path,
    ignored_dirs: set[str],
    ignored_files: set[str],
    ignored_prefixes: tuple[str, ...],
) -> bool:
    if rel_path.name in ignored_files:
        return True
    rel_posix = rel_path.as_posix()
    for prefix in ignored_prefixes:
        if rel_posix.startswith(prefix):
            return True
    for part in rel_path.parts:
        if part in ignored_dirs:
            return True
    return False


def compact_tscn(content: str) -> str:
    """Keep Godot .tscn node hierarchy, ext_resource, and signal connections while trimming large data arrays."""
    compacted: list[str] = []
    skip_sub_resource_data = False
    for line in content.splitlines():
        if (
            line.startswith("[node ")
            or line.startswith("[ext_resource ")
            or line.startswith("[connection ")
            or line.startswith("[gd_scene")
        ):
            skip_sub_resource_data = False
            compacted.append(line)
        elif line.startswith("[sub_resource "):
            skip_sub_resource_data = True
            compacted.append(line)
        elif not skip_sub_resource_data:
            if len(line) > 300:
                compacted.append(line[:300] + " ... [truncated data line]")
            else:
                compacted.append(line)
    return "\n".join(compacted)


def collect_repo_context(repo_root: Path, config: dict[str, Any]) -> tuple[str, dict[str, int]]:
    """Collect holistic repository context and calculate scale metrics."""
    ignored_dirs = set(DEFAULT_IGNORED_DIRS) | set(config.get("ignored_dirs", []))
    ignored_files = set(DEFAULT_IGNORED_FILES) | set(config.get("ignored_files", []))
    ignored_prefixes = tuple(list(DEFAULT_IGNORED_PATH_PREFIXES) + list(config.get("ignored_path_prefixes", [])))
    code_extensions = (
        set(config["code_extensions"])
        if "code_extensions" in config
        else set(DEFAULT_CODE_EXTENSIONS)
    )

    tree_lines: list[str] = []
    file_sections: list[str] = []
    total_lines = 0
    total_files = 0

    git_files_out = run_git(["ls-files", "--cached", "--others", "--exclude-standard"])
    if git_files_out.strip():
        all_paths = [repo_root / line.strip() for line in git_files_out.splitlines() if line.strip()]
    else:
        all_paths = sorted(repo_root.rglob("*"))

    for path in all_paths:
        if not path.is_file():
            continue
        rel = path.relative_to(repo_root)
        if should_ignore(rel, ignored_dirs, ignored_files, ignored_prefixes):
            continue

        rel_str = rel.as_posix()
        tree_lines.append(f"- {rel_str}")

        if path.suffix not in code_extensions and path.name not in SPECIAL_INCLUDED_FILENAMES:
            continue

        raw_text = path.read_text(encoding="utf-8", errors="ignore")
        raw_line_count = len(raw_text.splitlines())
        total_lines += raw_line_count
        total_files += 1

        display_text = compact_tscn(raw_text) if path.suffix == ".tscn" else raw_text
        file_sections.append(
            f"### File: `{rel_str}` ({raw_line_count} lines)\n```\n{display_text}\n```"
        )

    context_blob = (
        "## Repository File Tree\n"
        + "\n".join(tree_lines)
        + "\n\n## Repository Source Files\n\n"
        + "\n\n".join(file_sections)
    )
    est_tokens = max(1, len(context_blob) // 4)
    metrics = {
        "total_files": total_files,
        "total_lines": total_lines,
        "est_tokens": est_tokens,
    }
    return context_blob, metrics


def get_diff_and_valid_lines(
    base_sha: str,
    head_sha: str,
    config: dict[str, Any],
) -> tuple[str, dict[str, set[int]]]:
    """Return annotated diff with right-side line numbers and a map of valid (file -> set of lines) for inline comments."""
    ignored_files = set(DEFAULT_IGNORED_FILES) | set(config.get("ignored_files", []))
    exclude_args = ["--", ".", *(f":(exclude){fname}" for fname in sorted(ignored_files))]

    raw_diff = run_git(["diff", "--unified=3", f"{base_sha}...{head_sha}", *exclude_args])
    if not raw_diff.strip():
        raw_diff = run_git(["diff", "--unified=3", base_sha, head_sha, *exclude_args])

    annotated_lines: list[str] = []
    valid_lines: dict[str, set[int]] = {}
    current_file: str | None = None
    right_line_num = 0

    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    for line in raw_diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            valid_lines.setdefault(current_file, set())
            annotated_lines.append(line)
            continue
        if line.startswith("--- "):
            annotated_lines.append(line)
            continue

        hunk_match = hunk_re.match(line)
        if hunk_match:
            right_line_num = int(hunk_match.group(1))
            annotated_lines.append(line)
            continue

        if current_file is None:
            annotated_lines.append(line)
            continue

        if line.startswith("+") and not line.startswith("+++"):
            valid_lines[current_file].add(right_line_num)
            annotated_lines.append(f"[L{right_line_num}] {line}")
            right_line_num += 1
        elif line.startswith("-") and not line.startswith("---"):
            annotated_lines.append(f"     {line}")
        elif line.startswith(" "):
            valid_lines[current_file].add(right_line_num)
            annotated_lines.append(f"[L{right_line_num}] {line}")
            right_line_num += 1
        else:
            annotated_lines.append(line)

    return "\n".join(annotated_lines), valid_lines


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]
    return json.loads(cleaned)


def call_anthropic(api_key: str, model: str, system_prompt: str, user_prompt: str) -> dict[str, Any]:
    url = "https://api.anthropic.com/v1/messages"
    payload = {
        "model": model,
        "max_tokens": 4096,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["content"][0]["text"]
    return extract_json_object(text)


def call_openai(api_key: str, model: str, system_prompt: str, user_prompt: str) -> dict[str, Any]:
    url = "https://api.openai.com/v1/chat/completions"
    payload = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"]
    return extract_json_object(text)


REVIEW_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["APPROVE", "COMMENT", "NEEDS_ATTENTION"],
        },
        "headline": {"type": "string"},
        "holistic_impact": {"type": "string"},
        "strengths": {
            "type": "array",
            "items": {"type": "string"},
        },
        "risks_and_suggestions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "inline_comments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "body": {"type": "string"},
                },
                "required": ["path", "line", "body"],
            },
        },
    },
    "required": [
        "verdict",
        "headline",
        "holistic_impact",
        "strengths",
        "risks_and_suggestions",
        "inline_comments",
    ],
}


def call_gemini(api_key: str, model: str, system_prompt: str, user_prompt: str) -> dict[str, Any]:
    from google import genai

    client = genai.Client(api_key=api_key)
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        try:
            interaction = client.interactions.create(
                model=model,
                system_instruction=system_prompt,
                input=user_prompt,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": REVIEW_JSON_SCHEMA,
                },
            )
            return extract_json_object(interaction.output_text or "{}")
        except Exception as exc:
            err_str = str(exc)
            is_transient = any(
                code in err_str
                for code in ("503", "429", "high demand", "service_unavailable", "overloaded")
            )
            if not is_transient or attempt == max_attempts:
                raise
            wait_s = 10 * attempt
            print(
                f"Gemini transient error (attempt {attempt}/{max_attempts}): {err_str}. "
                f"Retrying in {wait_s}s..."
            )
            time.sleep(wait_s)
    return {}


def build_scale_meter_markdown(metrics: dict[str, int], config: dict[str, Any]) -> tuple[str, bool]:
    token_budget_limit = int(config.get("token_budget_limit", DEFAULT_TOKEN_BUDGET_LIMIT))
    line_budget_limit = int(config.get("line_budget_limit", DEFAULT_LINE_BUDGET_LIMIT))
    warning_ratio = float(config.get("warning_ratio", DEFAULT_WARNING_RATIO))

    est_tokens = metrics["est_tokens"]
    total_lines = metrics["total_lines"]
    total_files = metrics["total_files"]

    token_ratio = est_tokens / token_budget_limit
    line_ratio = total_lines / line_budget_limit
    usage_ratio = max(token_ratio, line_ratio)
    pct = min(100.0, usage_ratio * 100.0)
    is_near_limit = usage_ratio >= warning_ratio

    filled = min(10, int(round(usage_ratio * 10)))
    bar = "█" * filled + "░" * (10 - filled)
    status = "⚠️ Approaching Single-Pass Limit" if is_near_limit else "✅ Healthy (Single-Pass Mode)"

    meter_md = (
        "---\n"
        "### 📊 Holistic Repo Context Meter (Option 1 Capacity)\n"
        f"`[{bar}] {pct:.1f}%` — **{status}**\n"
        f"- **Scanned Files**: `{total_files}` | **Source Lines**: `{total_lines:,} / {line_budget_limit:,}` | "
        f"**Est. Context Tokens**: `~{est_tokens:,} / {token_budget_limit:,}`\n"
    )
    return meter_md, is_near_limit


def post_github_review(
    repo: str,
    pr_number: int,
    github_token: str,
    commit_id: str,
    body: str,
    inline_comments: list[dict[str, Any]],
) -> None:
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"
    payload: dict[str, Any] = {
        "commit_id": commit_id,
        "body": body,
        "event": "COMMENT",
    }
    if inline_comments:
        payload["comments"] = inline_comments

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        print(
            f"Posted GitHub PR review (HTTP {resp.status}) with "
            f"{len(inline_comments)} inline comment(s)."
        )


def main() -> int:
    repo_root = Path.cwd()
    config = load_repo_config(repo_root)

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    custom_model = os.environ.get("AI_REVIEW_MODEL", "").strip()

    if not (anthropic_key or openai_key or gemini_key):
        print(
            "SKIP: No AI API key found in GitHub Secrets "
            "(set ANTHROPIC_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY in Repository Secrets to enable AI PR reviews)."
        )
        return 0

    pr_number_str = os.environ.get("PR_NUMBER", "").strip()
    base_sha = os.environ.get("BASE_SHA", "").strip()
    head_sha = os.environ.get("HEAD_SHA", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    pr_title = os.environ.get("PR_TITLE", "").strip()
    pr_body = os.environ.get("PR_BODY", "").strip()

    system_prompt = build_system_prompt(repo_root, repo, config)
    context_blob, metrics = collect_repo_context(repo_root, config)
    annotated_diff, valid_lines = get_diff_and_valid_lines(base_sha, head_sha, config)

    if not annotated_diff.strip():
        print("No diff changes detected to review.")
        return 0

    user_prompt = (
        f"# Pull Request Metadata\n"
        f"- **Repository**: {repo or repo_root.name}\n"
        f"- **Title**: {pr_title}\n"
        f"- **Description**: {pr_body or '(No description provided)'}\n\n"
        f"# Annotated PR Diff (`[L<num>]` indicates right-side line number for inline comments)\n"
        f"```diff\n{annotated_diff}\n```\n\n"
        f"# Full Repository Context\n"
        f"{context_blob}\n"
    )

    try:
        if anthropic_key:
            provider_label = "Anthropic Claude"
            model_name = custom_model or "claude-sonnet-4-5"
            review_data = call_anthropic(anthropic_key, model_name, system_prompt, user_prompt)
        elif openai_key:
            provider_label = "OpenAI"
            model_name = custom_model or "gpt-4o"
            review_data = call_openai(openai_key, model_name, system_prompt, user_prompt)
        else:
            provider_label = "Google Gemini"
            model_name = custom_model or "gemini-3.8-flash"
            review_data = call_gemini(gemini_key, model_name, system_prompt, user_prompt)
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="ignore")
        print(f"ERROR calling AI provider ({exc.code}): {err_body}")
        return 1

    verdict = review_data.get("verdict", "COMMENT")
    headline = review_data.get("headline", "Holistic repository review completed.")
    holistic_impact = review_data.get("holistic_impact", "")
    strengths = review_data.get("strengths", [])
    risks = review_data.get("risks_and_suggestions", [])
    raw_inline = review_data.get("inline_comments", [])

    valid_inline_comments: list[dict[str, Any]] = []
    fallback_notes: list[str] = []

    for item in raw_inline:
        c_path = str(item.get("path", "")).strip()
        c_line = item.get("line")
        c_body = str(item.get("body", "")).strip()
        if not c_path or not c_body or not isinstance(c_line, int):
            continue
        if c_path in valid_lines and c_line in valid_lines[c_path]:
            valid_inline_comments.append(
                {
                    "path": c_path,
                    "line": c_line,
                    "side": "RIGHT",
                    "body": c_body,
                }
            )
        else:
            fallback_notes.append(f"- **`{c_path}` (line {c_line})**: {c_body}")

    meter_md, is_near_limit = build_scale_meter_markdown(metrics, config)

    verdict_badge = {
        "APPROVE": "✅ **Verdict: Looks Good (Approved)**",
        "NEEDS_ATTENTION": "⚠️ **Verdict: Needs Attention**",
    }.get(verdict, "💬 **Verdict: Reviewed with Notes**")

    sections: list[str] = [
        f"## 🤖 Holistic AI Code Review ({provider_label} `{model_name}`)",
        f"{verdict_badge} — {headline}",
    ]

    if is_near_limit:
        sections.append(
            "> [!WARNING]\n"
            "> **Repository Scale Alert**: This repository has reached **>=75%** of the single-pass context budget. "
            "It is time to enable **Dependency-Aware Context Slicing** in `jameslac/github-workflows` (`scripts/ai_pr_reviewer.py`) or switch to **Option 2 (Agentic CLI)**!"
        )

    if holistic_impact:
        sections.append(f"### 🌐 Holistic Repo Impact\n{holistic_impact}")

    if strengths:
        sections.append("### ✨ What Looks Good\n" + "\n".join(f"- {s}" for s in strengths))

    if risks:
        sections.append("### 🔍 Risks, Vulnerabilities & Suggestions\n" + "\n".join(f"- {r}" for r in risks))

    if fallback_notes:
        sections.append("### 📌 File-Specific Notes (Outside Diff Hunks)\n" + "\n".join(fallback_notes))

    sections.append(meter_md)
    full_body = "\n\n".join(sections)

    if repo and pr_number_str and github_token and head_sha:
        post_github_review(
            repo=repo,
            pr_number=int(pr_number_str),
            github_token=github_token,
            commit_id=head_sha,
            body=full_body,
            inline_comments=valid_inline_comments,
        )
    else:
        print(full_body)

    return 0


if __name__ == "__main__":
    sys.exit(main())
