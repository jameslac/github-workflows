# `jameslac/github-workflows` — Shared GitHub Actions & AI Holistic PR Reviewer

Central repository for reusable GitHub Actions workflows across `@jameslac` projects.

## Workflows at a glance

| Workflow | What it does | Trigger in the calling repo | Secrets / variables needed |
|---|---|---|---|
| [`ai-review.yml`](#-holistic-ai-pr-reviewer-ai-reviewyml) | AI review comments on each PR | `pull_request` | one AI key (`GEMINI_API_KEY`, `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`) |
| [`ai-daily-audit.yml`](#️-daily-ai-repo-audit--github-issues-ai-daily-auditsyml) | Daily audit that files GitHub issues | `schedule` + `workflow_dispatch` | an AI key; optional `AUDIT_FALLBACK_MODEL` variable |
| [`pr-merged-sync.yml`](#-pr-merged-sync-pr-merged-syncyml) | On merge: close linked issues, move Trello card to Done | `pull_request: [closed]` | optional `TRELLO_API_KEY`, `TRELLO_TOKEN` |
| [`cloudflare-preview-cleanup.yml`](#️-cloudflare-worker-preview-cleanup-cloudflare-preview-cleanupyml) | Delete a branch's Cloudflare Worker Preview | `pull_request: [closed]` + `delete` | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` |

### Prerequisites that apply to every workflow below

1. **Add the caller file to the default branch.** Scheduled runs, the **Run workflow** button, and `delete` events only work for workflow files on the default branch (usually `main`). PR-triggered callers work from the PR branch once the file exists in the base branch.
2. **Actions must be enabled** for the repo (**Settings → Actions → General**). Reusable workflows from `jameslac/github-workflows` are allowed by default for repos you own; if you restricted allowed actions, allow `jameslac/github-workflows/*`.
3. **Add secrets/variables** under **Settings → Secrets and variables → Actions** (the **Secrets** tab for keys and tokens; the **Variables** tab for plain settings such as `AUDIT_FALLBACK_MODEL`). Callers use `secrets: inherit`, so any secret on the repo is passed through. Variables are read by the central workflow directly.
4. **Keep the `permissions:` block in the caller.** A reusable workflow can never get more permissions than its caller grants, so the caller must list at least what the setup section below shows.
5. **To test,** open the **Actions** tab, pick the workflow, and click **Run workflow** (only for callers with `workflow_dispatch`), or run `gh workflow run <file>.yml --repo owner/name`.

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

3. Merge the caller file to the default branch, then open a non-draft PR. The review is posted as a PR review within a few minutes. Draft PRs are skipped until marked ready for review.

> Free-tier note: this workflow makes one large LLM call per PR push. If you hit rate limits, switch the repo to the [daily audit](#️-daily-ai-repo-audit--github-issues-ai-daily-auditsyml) instead.

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

### Setup steps

1. Create `.github/workflows/ai-daily-audit.yml` in the repo using the snippet above and merge it to the default branch.
2. Add the secret `GEMINI_API_KEY` (**Settings → Secrets and variables → Actions → Secrets**). `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` are used only if no Gemini key exists.
3. *(Recommended on the free tier)* On the **Variables** tab add `AUDIT_FALLBACK_MODEL` set to the exact API ID of a second Gemini model with its own quota (check AI Studio). Optionally add `AI_REVIEW_MODEL` to change the primary model.
4. Run it once: **Actions → AI Daily Audit → Run workflow** (or `gh workflow run ai-daily-audit.yml --repo owner/name`). The first run is a full baseline and can take several minutes because large repos are sent in chunks with pauses between them.
5. Check the run log for `Audit mode:`, then open the repo's **Issues** tab for `[AI Audit] ...` issues labeled `ai-audit` and `severity:*`.
6. Remove the old per-PR `ai-review.yml` caller if you are moving from per-PR reviews to the daily audit.

Optional inputs under `with:`: `max_issues` (default 5), `label` (default `ai-audit`), `full_audit` (force a full baseline), `force` (re-audit an already-audited commit), `model`, `central_ref`. Per-repo tuning goes in `.ai-review.json` (see above: `audit_token_budget`, `audit_incremental_token_budget`, `audit_max_total_issues`, `extra_instructions`, ignore lists).

> Downstream automations can pick these issues up by the `ai-audit` label. Fix PRs should contain `Closes #<issue number>` so that [PR Merged Sync](#-pr-merged-sync-pr-merged-syncyml) can close the issue on merge.

- **Morning EST/EDT scheduling**: GitHub Actions `schedule` cron uses **UTC** (`10:xx UTC` = 5:xx AM EST / 6:xx AM EDT; `11:xx UTC` = 6:xx AM EST / 7:xx AM EDT). Allow ~15–45 minutes for GitHub Actions cron queue jitter and stagger minutes/hours across repos so they don't share the same RPM/TPM window.
- **Provider & budgets**: Uses `GEMINI_API_KEY` first (then Anthropic/OpenAI). Incremental context is capped at ~80k tokens (`audit_incremental_token_budget` in `.ai-review.json`), and each full-baseline request is capped at ~100k tokens (`audit_token_budget`, estimated at ~3 chars/token). Larger repos are split into multiple chunked requests with a ~65s pause between them (`AUDIT_CHUNK_DELAY`) so per-minute free-tier token limits are never exceeded, with at most 10 issues filed per run (`audit_max_total_issues`). Gemini 429s are retried once after waiting out the rate-limit window; 503 "high demand" errors are retried up to 4 times with 30s/60s/90s/120s backoff. If the provider is still unavailable the run ends with a warning annotation (not a failure) and the audit state is not advanced, so the next run retries. Optionally set the repository variable `AUDIT_FALLBACK_MODEL` to a second Gemini model to try when the primary stays unavailable. Pass `with: full_audit: true` (or `workflow_dispatch`) to force a full baseline audit.

## 🔗 PR Merged Sync (`pr-merged-sync.yml`)

When a PR is merged: closes the issues it links (`Closes #123`), removes AI state labels (`ai-in-progress`, `ai-review`, `ai-fix` by default), and comments on them. If the PR title/body contains a Trello card URL (`https://trello.com/c/<id>`), it also moves that card to the list named `Done` (configurable) on the card's board. The Trello step is skipped unless the `TRELLO_API_KEY` and `TRELLO_TOKEN` secrets are set.

```yaml
name: PR Merged Sync

on:
  pull_request:
    types: [closed]

permissions:
  contents: read
  issues: write
  pull-requests: read

jobs:
  sync:
    uses: jameslac/github-workflows/.github/workflows/pr-merged-sync.yml@main
    secrets: inherit
    # with:
    #   trello_done_list: "Done"
```

### Setup steps

1. Create `.github/workflows/pr-merged-sync.yml` in the repo using the snippet above and merge it to the default branch.
2. **Issues:** write `Closes #123` (or `Fixes #123` / `Resolves #123`) in the PR description. Only issues in the same repository are closed. No secrets needed.
3. **Trello (optional):** add the secrets `TRELLO_API_KEY` and `TRELLO_TOKEN` (create them at <https://trello.com/power-ups/admin>; the token needs read/write access) and put the card link (`https://trello.com/c/<id>`) in the PR title or description. Make sure the card's board has an open list named `Done`, or pass `with: trello_done_list: "<list name>"`. Without the secrets the Trello step is skipped and the run still succeeds.
4. Merge a test PR that links an issue or card, then check the **Actions** run log (`Closed issue #N`, `Moved Trello card ... to 'Done'`).
5. To clear extra labels on close, pass `with: cleanup_labels: "label-a,label-b"` (default `ai-in-progress,ai-review,ai-fix`).

> Uses the `pull_request` event (not `pull_request_target`), so fork PRs never get secrets. Trello credentials are sent in an `Authorization` header, not in URLs.

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

3. Merge the caller file to the default branch, then close or merge a PR (or delete a branch) that had a preview and check the run log.

> `delete` events only fire for workflows on the default branch, so the caller workflow must be merged to `main` first.
