#!/usr/bin/env python3
"""
Daily AI Repo Audit -> GitHub Issues.

Runs once per day (scheduled in the caller repo). Makes ONE LLM call over the whole
repository context, and files a GitHub issue for each concrete, fixable finding.
Issues are labeled so a downstream automation (e.g. Trello/Antigravity) can pick them up.

Free-tier friendly:
- One request per repo per day (vs. one per PR push).
- Skips the LLM call entirely if the repo has no new commits since the last audit.
- Context is capped to a token budget.
- Capped number of issues per run; de-duplicates against existing open/closed audit issues.

Environment:
  GITHUB_TOKEN, GITHUB_REPOSITORY
  GEMINI_API_KEY | ANTHROPIC_API_KEY | OPENAI_API_KEY
  AI_REVIEW_MODEL (optional)
  AUDIT_LABEL (default: ai-audit), AUDIT_MAX_ISSUES (default: 5), AUDIT_FORCE (set to 1 to skip the no-change check)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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
FINGERPRINT_RE = re.compile(r"<!-- ai-audit-fingerprint:([0-9a-f]{12}) -->")
HEAD_RE = re.compile(r"<!-- ai-audit-head:([0-9a-f]{7,40}) -->")

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


def build_audit_prompt(repo_root: Path, repo_name: str, config: dict[str, Any], max_issues: int) -> str:
    project_label = config.get("project_name") or repo_name or repo_root.name
    stacks = core.detect_stack_summary(repo_root)
    stack_str = ", ".join(stacks) if stacks else "Software Engineering & Architecture"
    guidelines = core.load_architecture_guidelines(repo_root)
    extra = config.get("extra_instructions", [])
    extra_block = ("\n## Extra Instructions\n" + "\n".join(f"- {e}" for e in extra) + "\n") if extra else ""
    guidelines_block = (
        f"\n## Repository Rules (authoritative; flag violations)\n{guidelines}\n" if guidelines else ""
    )
    return f"""You are a Senior Principal Engineer auditing the whole repository "{project_label}" ({stack_str}).
Find concrete, high-confidence, FIXABLE problems and report each as a self-contained work item that an
autonomous coding agent can pick up and fix without further context.

Look for: real bugs, broken references/imports/paths, security/privacy issues (leaked secrets, unsafe input),
performance/resource problems, violations of the repository's documented architecture rules, and missing
validation or error handling.

Rules:
- Report at most {max_issues} issues, highest severity first. Fewer is better; return an empty list if the repo is clean.
- Each issue must reference real files that exist in the provided context, with enough detail (function names,
  line hints) to locate the problem. Never speculate or pad with style nitpicks.
- Each issue must be independently fixable in a single small PR.
- Do NOT report issues that match any "Already Tracked" item provided by the user.
- Today's environment is 2026: Godot 4.7, React 19, Vite 6, Tailwind v4, wrangler v4, Kotlin 2.x,
  actions/checkout@v5, actions/setup-python@v6, Google GenAI SDK `client.interactions.create`,
  `gemini-3.8-flash` and `gemini-4-argon` are valid and current. NEVER flag them as non-existent.
{extra_block}{guidelines_block}
Respond ONLY with JSON matching: {{"summary": str, "issues": [{{"title","severity","category","files","problem","suggested_fix","acceptance_criteria"}}]}}
"""


def render_issue_body(item: dict[str, Any], fingerprint: str, head: str, model_label: str) -> str:
    files = "\n".join(f"- `{f}`" for f in item.get("files", [])) or "- (unspecified)"
    criteria = "\n".join(f"- [ ] {c}" for c in item.get("acceptance_criteria", [])) or "- [ ] Issue resolved"
    return (
        f"**Severity:** `{item['severity']}` | **Category:** `{item['category']}`\n\n"
        f"## Problem\n{item['problem'].strip()}\n\n"
        f"## Affected files\n{files}\n\n"
        f"## Suggested fix\n{item['suggested_fix'].strip()}\n\n"
        f"## Acceptance criteria\n{criteria}\n\n"
        f"---\n_Filed automatically by the daily AI repo audit ({model_label}) at `{head[:7]}`._\n"
        f"<!-- ai-audit-fingerprint:{fingerprint} -->\n"
        f"<!-- ai-audit-head:{head} -->"
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
    max_issues = int(os.environ.get("AUDIT_MAX_ISSUES", config.get("audit_max_issues", DEFAULT_MAX_ISSUES)))
    force = os.environ.get("AUDIT_FORCE", "").strip() in ("1", "true")

    if not (repo and token):
        print("ERROR: GITHUB_REPOSITORY and GITHUB_TOKEN are required.")
        return 1
    if not any(os.environ.get(k, "").strip() for k in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")):
        print("SKIP: No AI API key configured.")
        return 0

    head = current_head()
    existing = fetch_existing_audit_issues(repo, token, label)

    # Skip if nothing changed since the last audit.
    if not force:
        for issue in existing:
            m = HEAD_RE.search(issue.get("body") or "")
            if m:
                if m.group(1) == head:
                    print(f"SKIP: HEAD {head[:7]} already audited (see #{issue['number']}). Set AUDIT_FORCE=1 to override.")
                    return 0
                break

    context_blob, metrics = core.collect_repo_context(repo_root, config)
    budget = int(config.get("audit_token_budget", DEFAULT_AUDIT_TOKEN_BUDGET))
    if metrics["est_tokens"] > budget:
        print(f"WARNING: context ~{metrics['est_tokens']:,} tokens exceeds budget {budget:,}; truncating.")
        context_blob = context_blob[: budget * 4]

    tracked_lines = [
        f"- [{i['state']}] #{i['number']}: {i['title']}" for i in existing[:100]
    ]
    tracked = "\n".join(tracked_lines) or "(none)"
    user_prompt = (
        f"# Already Tracked (do not duplicate)\n{tracked}\n\n"
        f"# Full Repository Context\n{context_blob}\n"
    )
    system_prompt = build_audit_prompt(repo_root, repo, config, max_issues)

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
                "body": render_issue_body(item, fp, head, model_label),
                "labels": labels,
            },
        )
        created += 1
        print(f"Created issue #{issue['number']}: {title}")

    if created == 0:
        # Record a marker so we don't re-audit an unchanged HEAD tomorrow... only possible via an issue,
        # so we rely on the schedule + cheap single call instead; nothing to file.
        print("No new issues filed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
