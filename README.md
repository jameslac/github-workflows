# `jameslac/github-workflows` — Shared GitHub Actions & AI Holistic PR Reviewer

Central repository for reusable GitHub Actions workflows across `@jameslac` projects.

## 🤖 Holistic AI PR Reviewer (`ai-review.yml`)

Scans the full repository context, dynamically reads `AGENTS.md` / `CLAUDE.md` / `docs/ARCHITECTURE.md`, auto-detects the tech stack (TypeScript/React/Cloudflare, Godot 4, Kotlin/Android, Python, etc.), and posts a holistic PR summary review + inline diff comments + single-pass context capacity meter.

### Quick Setup in Any Repo

1. Add `.github/workflows/ai-review.yml` to your repository:

```yaml
name: AI Holistic PR Review

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read
  pull-requests: write

jobs:
  review:
    uses: jameslac/github-workflows/.github/workflows/ai-review.yml@main
    secrets: inherit
```

2. Ensure at least one AI provider key is set in the repository's **Settings → Secrets and variables → Actions**:
   - `GEMINI_API_KEY` (default model: `gemini-3.8-flash`)
   - `ANTHROPIC_API_KEY` (default model: `claude-sonnet-4-5`)
   - `OPENAI_API_KEY` (default model: `gpt-4o`)
   - *(Optional)* Repository variable `AI_REVIEW_MODEL` (or `with: model: "..."` in the workflow) to override the model name.

### Optional Per-Repo Overrides (`.ai-review.json`)

Zero config is required—the script automatically reads `AGENTS.md` and `docs/ARCHITECTURE.md`. If a repository needs custom settings, place an optional `.ai-review.json` at the repo root:

```json
{
  "project_name": "My Custom Project",
  "extra_instructions": [
    "Enforce strict module boundaries between core/ and features/."
  ],
  "ignored_dirs": ["generated"],
  "ignored_files": ["large-snapshot.json"],
  "ignored_path_prefixes": ["addons/local_plugin"],
  "token_budget_limit": 80000,
  "line_budget_limit": 80000
}
```

## 🗓️ Daily AI Repo Audit → GitHub Issues (`ai-daily-audit.yml`)

Free-tier-friendly alternative to per-PR reviews: **at most one LLM call per repo per day** files a GitHub issue for each concrete, fixable finding (max 5/run, de-duplicated against existing open/closed issues, labeled `ai-audit` + `severity:*`). Each issue includes problem, affected files, suggested fix, and acceptance criteria so a downstream agent automation can pick it up.

- **Incremental by default**: The first run performs a full-repository baseline audit; subsequent runs cache the last successful commit SHA via `actions/cache` (including clean 0-issue runs, with filed issue markers as fallback) and send only the diff since the last successful run plus changed files and 1-hop related files. Days with no new code commits are skipped without calling an LLM.

Add `.github/workflows/ai-daily-audit.yml` to a repo (and remove the per-PR `ai-review.yml` caller to stop burning quota):

```yaml
name: AI Daily Audit

on:
  schedule:
    - cron: "17 10 * * *"   # 5:17 AM EST / 6:17 AM EDT (GitHub cron is UTC; stagger minutes per repo)
  workflow_dispatch:

permissions:
  contents: read
  issues: write

jobs:
  audit:
    uses: jameslac/github-workflows/.github/workflows/ai-daily-audit.yml@main
    secrets: inherit
    # with:
    #   max_issues: 3
    #   label: ai-audit
    #   full_audit: false
```

- **Morning EST/EDT scheduling**: GitHub Actions `schedule` cron uses **UTC** (`10:xx UTC` = 5:xx AM EST / 6:xx AM EDT; `11:xx UTC` = 6:xx AM EST / 7:xx AM EDT). Allow ~15–45 minutes for GitHub Actions cron queue jitter and stagger minutes/hours across repos so they don't share the same RPM/TPM window.
- **Provider & budgets**: Uses `GEMINI_API_KEY` first (then Anthropic/OpenAI). Incremental context is capped at ~80k tokens (`audit_incremental_token_budget` in `.ai-review.json`), and full baseline runs are capped at ~200k tokens (`audit_token_budget`). Pass `with: full_audit: true` (or `workflow_dispatch`) to force a full baseline audit.

## ☁️ Cloudflare Worker Preview Cleanup (`cloudflare-preview-cleanup.yml`)

Deletes the branch's Cloudflare **Worker Preview** (created by `wrangler preview`) when a PR is closed/merged or its branch is deleted. Idempotent: if the preview is already gone (e.g. both events fire), it succeeds. If the Cloudflare secrets are missing it warns and skips instead of failing.

### Quick Setup in Any Repo

1. Add `.github/workflows/cleanup-preview.yml`:

```yaml
name: Cleanup Cloudflare Preview

on:
  pull_request:
    types: [closed]
  delete:

permissions:
  contents: read

jobs:
  cleanup:
    uses: jameslac/github-workflows/.github/workflows/cloudflare-preview-cleanup.yml@main
    secrets: inherit
    # with:
    #   worker_name: my-worker      # default: `name` from the repo's wrangler config
    #   wrangler_version: "latest"  # only used if the repo has no local wrangler (needs >= 4.135.0)
```

2. Add repository secrets (**Settings → Secrets and variables → Actions**):
   - `CLOUDFLARE_API_TOKEN` — token with **Account → Workers Scripts → Edit** (e.g. the "Edit Cloudflare Workers" template)
   - `CLOUDFLARE_ACCOUNT_ID`

> `delete` events only fire for workflows on the default branch, so the caller workflow must be merged to `main` first.
