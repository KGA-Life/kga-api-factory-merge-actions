"""Thin GitHub REST client for the merge machinery — stdlib only (urllib), no PyGithub/requests.

Mirrors the ``driver`` package's I/O discipline: this is the ONLY module in ``merge`` that touches
the network, so the pure decision logic (``signals`` / ``gate`` / ``revert.decide_revert``) stays
unit-testable without it. Every orchestration path (``executor`` / ``revert``) takes a ``GitHubApi``
instance so tests can inject a fake and never hit GitHub.

Auth: a token from the ``GITHUB_TOKEN`` env var (in an Actions runner this is the job token —
``github-actions[bot]``, a NON-Claude-Code actor — or a passed-in narrowly-scoped Claudette PAT for
the cross-repo ``workflow_dispatch`` path). The token value is redacted from any error text.

Runtime: GitHub Actions (ubuntu-latest, python3.14). No AWS, no third-party deps.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

_API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
_UA = "kga-api-factory-merge"


class GitHubError(RuntimeError):
    """A non-2xx GitHub response. Carries the status so callers can branch (e.g. 409 on a stale
    merge SHA, 405 on an un-mergeable PR) rather than parse strings."""

    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class GitHubApi:
    """A minimal typed-ish wrapper over the REST endpoints the merge machinery needs.

    Only the verbs used by ``executor`` and ``revert`` are implemented; this is deliberately not a
    general client. Read verbs return the decoded JSON; write verbs return the decoded JSON of the
    created/updated resource (or ``{}`` for empty 204 bodies).
    """

    def __init__(self, token: str | None = None, *, api_url: str = _API):
        self._token = token or os.environ.get("GITHUB_TOKEN", "")
        self._api = api_url.rstrip("/")

    # --- transport -----------------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self._api}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", _UA)
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = self._redact(exc.read().decode(errors="replace")[:500])
            raise GitHubError(exc.code, detail) from None

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    # --- reads ---------------------------------------------------------------
    def get_pull(self, owner: str, repo: str, number: int) -> dict:
        return self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}")

    def list_issue_comments(self, owner: str, repo: str, number: int) -> list[dict]:
        # Paginate: the @claude review verdict is edited in place on ONE comment, but a busy PR
        # can push it past page 1, and "latest claude[bot] comment" must see every page.
        out: list[dict] = []
        page = 1
        while True:
            batch = self._request(
                "GET",
                f"/repos/{owner}/{repo}/issues/{number}/comments?per_page=100&page={page}",
            )
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return out

    def list_check_runs(self, owner: str, repo: str, ref: str) -> list[dict]:
        body = self._request("GET", f"/repos/{owner}/{repo}/commits/{ref}/check-runs?per_page=100")
        return body.get("check_runs", [])

    def get_combined_status(self, owner: str, repo: str, ref: str) -> dict:
        return self._request("GET", f"/repos/{owner}/{repo}/commits/{ref}/status")

    def list_pulls(self, owner: str, repo: str, *, state: str = "open", head: str | None = None) -> list[dict]:
        q = f"state={state}&per_page=100"
        if head:
            q += f"&head={head}"
        body = self._request("GET", f"/repos/{owner}/{repo}/pulls?{q}")
        return body if isinstance(body, list) else []

    # --- writes --------------------------------------------------------------
    def merge_pull(self, owner: str, repo: str, number: int, *, sha: str, merge_method: str = "merge") -> dict:
        # Passing ``sha`` makes GitHub itself refuse (409) if the head moved since we read it — a
        # server-side backstop under the client-side head-SHA re-check in executor.
        return self._request(
            "PUT",
            f"/repos/{owner}/{repo}/pulls/{number}/merge",
            {"sha": sha, "merge_method": merge_method},
        )

    def create_pull(self, owner: str, repo: str, *, title: str, head: str, base: str, body: str) -> dict:
        return self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            {"title": title, "head": head, "base": base, "body": body},
        )

    def create_comment(self, owner: str, repo: str, number: int, body: str) -> dict:
        return self._request(
            "POST", f"/repos/{owner}/{repo}/issues/{number}/comments", {"body": body}
        )

    def add_labels(self, owner: str, repo: str, number: int, labels: list[str]) -> dict:
        return self._request(
            "POST", f"/repos/{owner}/{repo}/issues/{number}/labels", {"labels": labels}
        )

    def remove_label(self, owner: str, repo: str, number: int, label: str) -> dict:
        try:
            return self._request(
                "DELETE", f"/repos/{owner}/{repo}/issues/{number}/labels/{label}"
            )
        except GitHubError as exc:
            if exc.status == 404:  # label already absent — removing is idempotent
                return {}
            raise
