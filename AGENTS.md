# AGENTS.md — `jameslac/github-workflows`

## ⚠️ Public Repository Policy (Mandatory Review)

This repository is **PUBLIC** on GitHub (and must remain public so reusable workflows and scripts can be fetched by calling repositories). **Every file, workflow, script, comment, documentation change, and commit message added here is publicly visible.**

Before adding, editing, committing, or opening a PR for anything in this repository, you **MUST** review all changes against the checklist below:

1. **No secrets, keys, or credentials**
   - Never hardcode API keys, tokens, passwords, account IDs (e.g. Cloudflare account IDs), private keys, or `.env` files.
   - Always pass credentials via GitHub `${{ secrets.* }}` and environment variables—never in URLs, CLI flags that appear in logs, or default values.
   - Ensure error logging never prints secret values.
2. **No private project or internal details**
   - Do not include private repository names, internal URLs/domains, proprietary architecture/directory structures, internal plugin names, or personal workflow/automation specifics in shared workflows, scripts, comments, or prompts.
   - Keep repository-specific rules, ignored paths, and conventions in each calling repository's own `AGENTS.md` or `.ai-review.json`.
3. **No personal information (PII)**
   - Avoid personal email addresses, local machine paths (e.g. `/Users/...`), or personal identifiers in code, docs, and examples. Use generic placeholders (`my-worker`, `owner/repo`).
   - Prefer GitHub's noreply email address in git commit config when committing to this repo.
4. **Reusable GitHub Actions security hygiene**
   - Never interpolate untrusted `${{ github.event.* }}` or `${{ inputs.* }}` directly inside `run:` shell commands; pass them through step `env:` variables and quote them in shell scripts.
   - Use least-privilege `permissions:` blocks and guard against untrusted fork PRs where applicable.
   - Do not recommend calling these workflows from `pull_request_target` with untrusted checkout.
5. **Diff audit before finishing**
   - Inspect `git status`, `git diff`, and any newly created files specifically for accidental public exposure before committing or pushing.

## Repository Conventions

- Reusable workflows live in `.github/workflows/`; supporting scripts live in `scripts/`.
- Update `README.md` whenever adding a workflow or changing inputs, secrets, or behavior.
- Keep this file named `AGENTS.md` at the repo root so automated reviewers (`scripts/ai_pr_reviewer.py`, `scripts/ai_repo_audit.py`) automatically load and enforce these rules on Linux runners.
