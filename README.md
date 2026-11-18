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
  "token_budget_limit": 80000,
  "line_budget_limit": 80000
}
```

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
