"""T47 — post-merge verification and auto-revert. The third layer of the defense chain: even a
correct three-signal gate can wrongly greenlight, so nothing is final until the default branch is
re-verified on the merge commit. On red, the merge is auto-reverted and the issue reopened, so a
"Done" issue always corresponds to a healthy ``main``. The revert PR is squash-merged (KGA-395) so
the fix lands as a GitHub-signed (Verified) commit on ``main``, and its branch is deleted afterwards.

Runs as a GitHub Action on push to the default branch (wired at T35), or via ``workflow_dispatch``.

Idempotency (the flapping-check guard): a post-merge check can flap red→green→red across retries or
a redelivered event. The revert is keyed on the MERGE COMMIT SHA — the revert branch is
``revert/<short-merge-sha>`` — and before opening one we check whether a revert PR (open OR already
landed) for that SHA exists. One merge commit yields at most one revert, however many times the
check flaps.

Linear reopen: T47 must reopen/annotate the Linear issue on revert. The Action is not yet
provisioned with a Linear token, so ``reopen_issue`` is a clean injectable seam — absent a callback
it records a "manual Linear reopen needed" annotation on GitHub and reports ``skipped_no_token``,
rather than shipping an unexercised GraphQL path. Wiring a real callback is a tracked follow-up.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

from . import signals
from .github_api import GitHubApi, GitHubError

REVERT_BRANCH_PREFIX = "revert/"
# The post-merge-verify workflow's own check on the merge commit — excluded from the CI poll so it
# never waits on itself (that would deadlock: its own run stays in_progress until this code exits).
VERIFY_CHECK_NAME = os.environ.get("POST_MERGE_VERIFY_CHECK_NAME", "post-merge-verify")
_POLL_ATTEMPTS = int(os.environ.get("POST_MERGE_POLL_ATTEMPTS", "20"))
_POLL_INTERVAL = int(os.environ.get("POST_MERGE_POLL_INTERVAL", "15"))
_GIT_NAME = os.environ.get("GIT_AUTHOR_NAME", "github-actions[bot]")
_GIT_EMAIL = os.environ.get("GIT_AUTHOR_EMAIL", "41898282+github-actions[bot]@users.noreply.github.com")


def revert_branch_name(merge_sha: str) -> str:
    return f"{REVERT_BRANCH_PREFIX}{merge_sha[:12]}"


# --- pure decision -----------------------------------------------------------
def decide_revert(*, ci_green: bool, existing_revert: bool, merge_sha: str) -> dict:
    """Decide whether to revert. Green -> stand. Already-reverting/reverted -> no-op (idempotent on
    the merge SHA). Otherwise -> revert."""
    if ci_green:
        return {"action": "none", "reason": f"default branch green on merge commit {merge_sha[:12]}"}
    if existing_revert:
        return {"action": "none", "reason": f"a revert for {merge_sha[:12]} already exists (idempotent no-op)"}
    return {"action": "revert", "reason": f"default branch red on merge commit {merge_sha[:12]}"}


# --- thin I/O ----------------------------------------------------------------
def _poll_ci(api, owner, repo, sha, *, ignore, sleep, self_run_id=None, attempts=_POLL_ATTEMPTS, interval=_POLL_INTERVAL):
    """Poll check-runs on ``sha`` until every relevant run has concluded, or ``attempts`` is
    exhausted. Returns ``(green, evidence)``. A timeout with runs still pending is reported as
    not-green (the evidence lists the still-pending runs).

    Self-exclusion is belt-and-suspenders so this workflow never deadlocks waiting on its OWN
    still-running check: by name substring (``ignore``), AND — name-independently, robust across the
    dispatch vs workflow_call paths — by dropping any run whose ``details_url`` carries this Actions
    run id (``self_run_id`` = ``$GITHUB_RUN_ID``)."""
    green, ev = False, {"reason": "no poll performed"}
    for _ in range(attempts):
        runs = api.list_check_runs(owner, repo, sha)
        if self_run_id:
            runs = [r for r in runs if self_run_id not in (r.get("details_url") or "")]
        green, ev = signals.ci_green_for_sha(
            runs,
            api.get_combined_status(owner, repo, sha),
            sha,
            ignore_check_names=ignore,
        )
        if not ev.get("pending_runs"):
            return green, ev
        sleep(interval)
    return green, ev


def _existing_revert(api, owner, repo, branch) -> bool:
    """True if a PR (open or already merged/closed) with the deterministic revert branch as its head
    already exists — the idempotency check. Uses the server-side ``head=`` filter so GitHub returns
    only PRs for this exact branch (no reliance on recency + a single unpaginated page — an older
    revert in a busy repo must still be found)."""
    head = f"{owner}:{branch}"
    for state in ("open", "all"):
        for pull in api.list_pulls(owner, repo, state=state, head=head):
            if (pull.get("head") or {}).get("ref") == branch:
                return True
    return False


def _default_git_revert_and_push(merge_sha: str, branch: str, base: str) -> None:
    def git(*args):
        subprocess.run(["git", *args], check=True, capture_output=True, text=True)

    git("config", "user.name", _GIT_NAME)
    git("config", "user.email", _GIT_EMAIL)
    git("checkout", base)
    git("checkout", "-b", branch)
    git("revert", "-m", "1", "--no-edit", merge_sha)
    git("push", "origin", branch)


def run(
    api: GitHubApi,
    owner: str,
    repo: str,
    *,
    merge_sha: str,
    base_branch: str = "main",
    pr_number: int | None = None,
    issue_id: str | None = None,
    git_revert_and_push=_default_git_revert_and_push,
    reopen_issue=None,
    sleep=time.sleep,
) -> dict:
    """Verify ``main`` on the merge commit and auto-revert if red. Returns a structured outcome."""
    # Exclude BOTH this workflow's own run (VERIFY_CHECK_NAME) AND the AI-review runs — otherwise the
    # always-failing `claude-review` check would make the post-merge poll read a healthy merge as red
    # and auto-revert it (KGA-334). Only the deterministic CI decides.
    green, ci_ev = _poll_ci(
        api, owner, repo, merge_sha,
        ignore=(VERIFY_CHECK_NAME, *signals.AI_REVIEW_CHECK_NAMES),
        sleep=sleep, self_run_id=os.environ.get("GITHUB_RUN_ID"),
    )
    branch = revert_branch_name(merge_sha)
    existing = _existing_revert(api, owner, repo, branch)
    decision = decide_revert(ci_green=green, existing_revert=existing, merge_sha=merge_sha)

    outcome = {"decision": decision, "ci": ci_ev, "merge_sha": merge_sha, "revert_branch": branch}
    if decision["action"] == "none":
        return {"outcome": "stood" if green else "noop_existing_revert", **outcome}

    # --- perform the revert (idempotent by construction on the branch name) ---------------------
    git_revert_and_push(merge_sha, branch, base_branch)
    revert_pr = api.create_pull(
        owner,
        repo,
        title=f"Revert merge {merge_sha[:12]} — post-merge check red (T47)",
        head=branch,
        base=base_branch,
        body=_revert_body(merge_sha, issue_id, ci_ev),
    )
    revert_head = (revert_pr.get("head") or {}).get("sha")
    # KGA-395: squash-merge the revert too, so the fix lands as a GitHub-signed (Verified) commit on
    # the default branch — the locally-created `git revert` commit is unsigned, and squashing keeps it
    # off `main`. Then delete the revert branch so that unsigned commit doesn't linger.
    merged = api.merge_pull(owner, repo, revert_pr["number"], sha=revert_head, merge_method="squash")
    _delete_revert_branch(api, owner, repo, branch)

    reopen = _reopen(issue_id, merge_sha, reopen_issue)
    if pr_number is not None:
        _annotate(api, owner, repo, pr_number, merge_sha, revert_pr["number"], reopen)

    return {
        "outcome": "reverted",
        **outcome,
        "revert_pr": revert_pr.get("number"),
        "revert_merge_commit": merged.get("sha"),
        "linear_reopen": reopen["status"],
    }


def _delete_revert_branch(api, owner, repo, branch) -> None:
    """Best-effort delete of the revert branch after its squash-merge (KGA-395). The locally-created,
    unsigned ``git revert`` commit lives only on this branch, so removing it keeps the repo free of
    unverified commits. Non-fatal — the revert has already landed; ``delete_ref`` swallows a 404/422."""
    try:
        api.delete_ref(owner, repo, branch)
    except GitHubError as exc:
        print(f"[merge:revert] WARNING could not delete revert branch {branch}: {exc}")


def _reopen(issue_id, merge_sha, reopen_issue) -> dict:
    if not issue_id:
        return {"status": "skipped_no_issue", "detail": "no Linear issue id supplied"}
    reason = f"merge {merge_sha[:12]} auto-reverted: post-merge default-branch check was red"
    if reopen_issue is None:
        # No Linear token wired into the Action yet — record the gap loudly instead of silently
        # leaving the issue Done on top of a reverted merge.
        print(f"[merge:revert] MANUAL LINEAR REOPEN NEEDED for {issue_id}: {reason}")
        return {"status": "skipped_no_token", "detail": reason, "issue": issue_id}
    reopen_issue(issue_id, reason)
    return {"status": "reopened", "detail": reason, "issue": issue_id}


def _annotate(api, owner, repo, pr_number, merge_sha, revert_pr, reopen) -> None:
    body = (
        f"⛔ **Post-merge check red — merge auto-reverted (T47).** "
        f"Merge commit `{merge_sha[:12]}` broke the default branch; reverted via #{revert_pr}.\n\n"
        f"- Linear reopen: `{reopen['status']}` — {reopen.get('detail', '')}"
    )
    try:
        api.create_comment(owner, repo, pr_number, body)
    except GitHubError as exc:
        print(f"[merge:revert] WARNING could not annotate PR #{pr_number}: {exc}")


def _revert_body(merge_sha: str, issue_id: str | None, ci_ev: dict) -> str:
    closes = f"\n\nRe-opens {issue_id} (post-merge revert)." if issue_id else ""
    return (
        f"Automated revert of merge commit `{merge_sha}` — the post-merge verification on the "
        f"default branch came back red, so the merge is reverted to restore last-known-good "
        f"(T47).{closes}\n\n"
        f"<details><summary>post-merge CI evidence</summary>\n\n```json\n"
        f"{json.dumps(ci_ev, indent=2, sort_keys=True)}\n```\n</details>"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Post-merge verify + auto-revert (T47)")
    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--merge-sha", required=True)
    ap.add_argument("--base", default="main")
    ap.add_argument("--pr", type=int, default=None, help="the original merged PR, for annotation")
    ap.add_argument("--issue", default=None, help="the Linear issue id, e.g. KGA-204")
    args = ap.parse_args(argv)

    api = GitHubApi()
    outcome = run(
        api, args.owner, args.repo,
        merge_sha=args.merge_sha, base_branch=args.base, pr_number=args.pr, issue_id=args.issue,
    )
    print(json.dumps(outcome, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
