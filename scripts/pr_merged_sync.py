#!/usr/bin/env python3
"""
When a pull request is merged:

1. Close any issues the PR links to (GitHub "closing" references such as `Closes #12`), remove
   AI-workflow state labels from them, and leave a short comment. GitHub already closes linked
   issues when a PR merges into the default branch; this makes it explicit, also covers merges into
   other branches, and cleans up labels.
2. If the PR body/title links a Trello card (`https://trello.com/c/<id>`), move that card to the
   configured "done" list on the card's own board and comment the PR URL. Skipped when Trello
   credentials are not configured.

Environment:
  GITHUB_TOKEN, GITHUB_REPOSITORY, PR_NUMBER
  CLEANUP_LABELS      comma-separated labels to remove from closed issues
                      (default: ai-in-progress,ai-review,ai-fix)
  TRELLO_API_KEY, TRELLO_TOKEN   optional; enable the Trello step
  TRELLO_DONE_LIST    name of the destination list (default: Done)
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

GITHUB_API = "https://api.github.com"
TRELLO_API = "https://api.trello.com/1"
TRELLO_CARD_RE = re.compile(r"https://trello\.com/c/([A-Za-z0-9]{6,})")


def http_json(req: urllib.request.Request, timeout: int = 30) -> Any:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else None


def gh(method: str, path: str, token: str, payload: dict[str, Any] | None = None) -> Any:
    url = path if path.startswith("http") else f"{GITHUB_API}{path}"
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
    return http_json(req)


def linked_closing_issues(repo: str, pr_number: int, token: str) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    query = """
    query($owner:String!, $name:String!, $number:Int!) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$number) {
          closingIssuesReferences(first: 20) {
            nodes { number state repository { nameWithOwner } }
          }
        }
      }
    }"""
    data = gh(
        "POST",
        "/graphql",
        token,
        {"query": query, "variables": {"owner": owner, "name": name, "number": pr_number}},
    )
    nodes = (
        (((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest") or {}
    ).get("closingIssuesReferences", {}).get("nodes", [])
    # Only act on issues in this same repository.
    return [n for n in nodes if n.get("repository", {}).get("nameWithOwner", "").lower() == repo.lower()]


def close_linked_issues(repo: str, pr: dict[str, Any], token: str, cleanup_labels: list[str]) -> None:
    number = pr["number"]
    pr_url = pr.get("html_url", "")
    for issue in linked_closing_issues(repo, number, token):
        n = issue["number"]
        if issue.get("state") == "OPEN":
            gh("PATCH", f"/repos/{repo}/issues/{n}", token, {"state": "closed", "state_reason": "completed"})
            print(f"Closed issue #{n}.")
        for label in cleanup_labels:
            try:
                gh("DELETE", f"/repos/{repo}/issues/{n}/labels/{urllib.parse.quote(label)}", token)
            except urllib.error.HTTPError as exc:
                if exc.code != 404:  # label not present
                    print(f"WARNING: could not remove label '{label}' from #{n}: HTTP {exc.code}")
        try:
            gh("POST", f"/repos/{repo}/issues/{n}/comments", token, {"body": f"Resolved by merged PR {pr_url}."})
        except urllib.error.HTTPError as exc:
            print(f"WARNING: could not comment on #{n}: HTTP {exc.code}")


def trello(method: str, endpoint: str, key: str, token: str, params: dict[str, str] | None = None) -> Any:
    # Credentials go in the Authorization header, never in the URL, so they cannot leak via logs.
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    req = urllib.request.Request(
        f"{TRELLO_API}/{endpoint.lstrip('/')}{query}",
        headers={"Authorization": f'OAuth oauth_consumer_key="{key}", oauth_token="{token}"'},
        method=method,
    )
    return http_json(req, timeout=20)


def normalize(text: str) -> str:
    return " ".join(text.strip().split()).lower()


def move_trello_cards(pr: dict[str, Any], key: str, token: str, done_list: str) -> None:
    text = f"{pr.get('title', '')}\n{pr.get('body') or ''}"
    card_ids = list(dict.fromkeys(TRELLO_CARD_RE.findall(text)))
    if not card_ids:
        print("No Trello card linked in PR; skipping Trello step.")
        return
    for card_id in card_ids:
        try:
            card = trello("GET", f"cards/{card_id}", key, token, {"fields": "idBoard,idList,name,closed"})
            lists = trello("GET", f"boards/{card['idBoard']}/lists", key, token, {"filter": "open", "fields": "id,name"})
            target = next((lst for lst in lists if normalize(lst["name"]) == normalize(done_list)), None)
            if target is None:
                print(f"WARNING: board for card {card_id} has no open list named '{done_list}'.")
                continue
            if card.get("idList") != target["id"]:
                trello("PUT", f"cards/{card_id}", key, token, {"idList": target["id"]})
                print(f"Moved Trello card {card_id} to '{target['name']}'.")
            trello(
                "POST",
                f"cards/{card_id}/actions/comments",
                key,
                token,
                {"text": f"PR merged: {pr.get('html_url', '')}"},
            )
        except urllib.error.HTTPError as exc:
            print(f"WARNING: Trello step failed for card {card_id}: HTTP {exc.code}")
        except Exception as exc:  # noqa: BLE001 - never leak credentials via exception text
            print(f"WARNING: Trello step failed for card {card_id}: {type(exc).__name__}")


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    pr_number = os.environ.get("PR_NUMBER", "").strip()
    if not (repo and token and pr_number.isdigit()):
        print("ERROR: GITHUB_REPOSITORY, GITHUB_TOKEN and numeric PR_NUMBER are required.")
        return 1

    pr = gh("GET", f"/repos/{repo}/pulls/{pr_number}", token)
    if not pr.get("merged"):
        print(f"PR #{pr_number} is not merged; nothing to do.")
        return 0

    cleanup = [x.strip() for x in os.environ.get("CLEANUP_LABELS", "ai-in-progress,ai-review,ai-fix").split(",") if x.strip()]
    close_linked_issues(repo, pr, token, cleanup)

    key = os.environ.get("TRELLO_API_KEY", "").strip()
    trello_token = os.environ.get("TRELLO_TOKEN", "").strip()
    if key and trello_token:
        move_trello_cards(pr, key, trello_token, os.environ.get("TRELLO_DONE_LIST", "").strip() or "Done")
    else:
        print("Trello credentials not configured; skipping Trello step.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
