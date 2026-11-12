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
