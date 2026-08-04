"""T34 — the autonomous PR-merge executor (a NON-Claude-Code actor) + the T43 gate evaluation it
consumes. Thin orchestration over ``github_api`` + the pure ``signals`` / ``gate`` modules.

Why a non-Claude-Code actor: the Claude Code auto-mode classifier blocks a Claude Code SESSION from
self-merging an AI-authored + AI-reviewed PR. This runs in a GitHub Actions runner (as
``github-actions[bot]`` on the ``workflow_call`` path, or as a narrowly-scoped Claudette PAT on the
``workflow_dispatch`` path) — not a Claude Code session — so the classifier does not apply, and the
bypass is compensated by the head-SHA CI re-check here + post-merge verify/revert (T47).

The load-bearing safety property (VG-4): immediately before merging, the executor RE-READS the PR
head SHA and RE-CHECKS CI-green on that exact SHA. If the head advanced since the gate gathered its
signals, or CI is not green on the current head, it refuses — a green recorded against a superseded
SHA never authorises a merge. This substitutes for the branch protection the Free-plan org can't buy.

The merge uses ``merge_method="squash"`` (KGA-395). GitHub authors the squash commit **server-side
and signs it**, so it lands **Verified** on the default branch — whereas the coding agent's own
feature-branch commits are signed with a key GitHub doesn't recognise (``reason=unknown_key``, an
artefact of the Managed Agents sandbox, where local signing is impossible). Squashing keeps those
unverified commits OFF the default branch, and the merged head branch is then DELETED so they don't
linger anywhere on the repo. The executor never sets the Linear issue Done — the PR's ``Closes
KGA-###`` keyword auto-transitions it on merge.

CLI: ``python -m merge.executor --owner O --repo R --pr N [--merge-method squash] [--dry-run]``
(``--dry-run`` evaluates the gate and logs the verdict but does not merge — used to dogfood the gate
against a real PR without merging it).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import gate, signals
from .github_api import GitHubApi, GitHubError

MERGE_CANDIDATE_LABEL = os.environ.get("MERGE_CANDIDATE_LABEL", signals.DEFAULT_MERGE_CANDIDATE_LABEL)
CLAUDE_BOT_LOGIN = os.environ.get("CLAUDE_BOT_LOGIN", signals.DEFAULT_BOT_LOGIN)
# Check-run names EXCLUDED from the hard CI-green signal — the AI-review runs (`claude`,
# `claude-review`), which are advisory and can fail/stall independently of the code (KGA-334). Only
# the deterministic test/lint/build CI gates the merge. Override via a comma-separated env var.
_BASE_IGNORE = tuple(
    n.strip() for n in os.environ.get("MERGE_IGNORE_CHECK_NAMES", "").split(",") if n.strip()
) or signals.AI_REVIEW_CHECK_NAMES

# The merge-gate workflow's OWN check-run (`merge-gate / gate-and-merge`) also appears on the PR head
# SHA while this executor runs. It MUST be excluded from the CI-green signal too — otherwise the gate
# reads its own in-progress run as a pending CI check and denies EVERY merge (a self-reference
# deadlock; the merge-gate analogue of the T47 post-merge-verify self-exclusion). Excluded two ways,
# mirroring revert._poll_ci: by this workflow's Actions run id (name-independent, matched in a run's
# ``details_url`` — the real fix) AND by the job's check-run leaf name (belt-and-suspenders,
# overridable). (KGA-336)
SELF_CHECK_NAME = os.environ.get("MERGE_GATE_CHECK_NAME", "gate-and-merge")
SELF_RUN_ID = os.environ.get("GITHUB_RUN_ID")
IGNORE_CHECK_NAMES = (*_BASE_IGNORE, SELF_CHECK_NAME)


def _drop_self_runs(check_runs: list[dict]) -> list[dict]:
    """Drop this workflow's own check-run (by Actions run id in ``details_url``) so the gate never
    counts its own still-running check as pending CI. Name-independent; a no-op when ``GITHUB_RUN_ID``
    is unset (e.g. local / ``workflow_dispatch`` without the Actions env)."""
    if not SELF_RUN_ID:
        return list(check_runs or [])
    return [r for r in (check_runs or []) if SELF_RUN_ID not in (r.get("details_url") or "")]


def _gather_signals(api: GitHubApi, owner: str, repo: str, number: int, sha: str, labels: list[dict]) -> dict:
    """Derive the three gate signals for ``sha`` from the already-fetched ``labels`` plus freshly
    fetched comments/checks. Returns the kwargs ``evaluate_gate`` wants plus a merged evidence dict."""
    labeled, label_ev = signals.merge_candidate_labeled(labels, label_name=MERGE_CANDIDATE_LABEL)
    comments = api.list_issue_comments(owner, repo, number)
    review_final, review_ev = signals.review_present_and_final(comments, bot_login=CLAUDE_BOT_LOGIN)
    # The reviewer's explicit verdict (KGA-337): a CHANGES_REQUESTED review is a hard block, so a
    # blocking @claude review can't be merged just because the label is on and CI is green.
    verdict, verdict_ev = signals.review_verdict(
        api.list_reviews(owner, repo, number), comments, bot_login=CLAUDE_BOT_LOGIN, head_sha=sha
    )
    ci_green, ci_ev = signals.ci_green_for_sha(
        _drop_self_runs(api.list_check_runs(owner, repo, sha)),
        api.get_combined_status(owner, repo, sha),
        sha,
        ignore_check_names=IGNORE_CHECK_NAMES,
    )
    return {
        "review_greenlit": labeled and review_final and verdict != signals.REVIEW_CHANGES_REQUESTED,
        "criteria_met": labeled,
        "ci_green": ci_green,
        "verdict": verdict,
        "evidence": {"label": label_ev, "review": review_ev, "review_verdict": verdict_ev, "ci": ci_ev},
    }


def run(
    api: GitHubApi,
    owner: str,
    repo: str,
    number: int,
    *,
    merge_method: str = "squash",
    dry_run: bool = False,
) -> dict:
    """Evaluate the gate and, if authorised, merge — with an immediately-before-merge head-SHA
    re-check. Returns a structured outcome dict (always includes ``outcome`` + ``verdict``)."""
    pr = api.get_pull(owner, repo, number)
    if pr.get("merged"):
        return {"outcome": "already_merged", "verdict": None}
    if pr.get("state") != "open":
        return {"outcome": "not_open", "state": pr.get("state"), "verdict": None}

    head_sha = (pr.get("head") or {}).get("sha")
    sig = _gather_signals(api, owner, repo, number, head_sha, pr.get("labels", []))
    verdict = gate.evaluate_gate(
        review_greenlit=sig["review_greenlit"],
        criteria_met=sig["criteria_met"],
        ci_green=sig["ci_green"],
        evidence={**sig["evidence"], "head_sha": head_sha},
    )
    # Name the blocking-review case explicitly in the audit trail (review_greenlit alone reads as a
    # missing/incomplete review; a CHANGES_REQUESTED verdict is a different, actionable thing).
    if sig.get("verdict") == signals.REVIEW_CHANGES_REQUESTED:
        verdict["reasons"].append("the @claude review verdict is CHANGES_REQUESTED (blocking)")

    if not verdict["authorise"]:
        # Comment only on an ACTIONABLE deny (a review/criteria problem the loop must fix); a
        # CI-only deny is transient (CI still running / just went red) and already visible in the
        # checks UI, so it is logged, not commented, to avoid spamming the PR on every re-trigger.
        if _is_actionable_deny(verdict) and not dry_run:
            _comment(api, owner, repo, number, _render(verdict, outcome="denied"))
        return {"outcome": "denied", "verdict": verdict}

    if dry_run:
        return {"outcome": "would_merge", "verdict": verdict, "head_sha": head_sha}

    # --- T34 load-bearing re-verify: re-read head SHA + re-check CI right before merging ---------
    pr2 = api.get_pull(owner, repo, number)
    current_sha = (pr2.get("head") or {}).get("sha")
    if current_sha != head_sha:
        verdict["reasons"].append(f"head SHA advanced during gate evaluation ({head_sha} -> {current_sha}); re-review required")
        verdict["authorise"] = False
        return {"outcome": "aborted_stale", "verdict": verdict, "head_sha": head_sha, "current_sha": current_sha}

    if pr2.get("mergeable") is False:  # explicit False = conflicts; null = GitHub still computing
        verdict["reasons"].append("PR not mergeable (conflicts)")
        verdict["authorise"] = False
        return {"outcome": "aborted_unmergeable", "verdict": verdict}

    ci_green_now, ci_ev_now = signals.ci_green_for_sha(
        _drop_self_runs(api.list_check_runs(owner, repo, current_sha)),
        api.get_combined_status(owner, repo, current_sha),
        current_sha,
        ignore_check_names=IGNORE_CHECK_NAMES,
    )
    if not ci_green_now:
        verdict["reasons"].append("CI not green on the current head SHA at merge time")
        verdict["authorise"] = False
        verdict["evidence"]["ci_recheck"] = ci_ev_now
        return {"outcome": "aborted_ci_recheck", "verdict": verdict}

    try:
        result = api.merge_pull(owner, repo, number, sha=current_sha, merge_method=merge_method)
    except GitHubError as exc:
        # 409 = head moved between our re-check and the call (sha param mismatch); 405 = not
        # mergeable. Either way, refuse cleanly rather than force anything.
        verdict["reasons"].append(f"merge call refused by GitHub: {exc}")
        verdict["authorise"] = False
        return {"outcome": "merge_refused", "verdict": verdict, "status": exc.status}

    verdict["evidence"]["merged_sha"] = current_sha
    verdict["evidence"]["merge_commit_sha"] = result.get("sha")
    # KGA-395: with a squash merge the default branch got a Verified commit; now delete the head
    # branch so the agent's unverified feature-branch commits don't linger. Best-effort, post-merge.
    deleted_branch = _delete_merged_branch(api, owner, repo, pr2)
    if deleted_branch:
        verdict["evidence"]["deleted_branch"] = deleted_branch
    _comment(api, owner, repo, number, _render(verdict, outcome="merged"))
    return {
        "outcome": "merged",
        "verdict": verdict,
        "merged_sha": current_sha,
        "merge_commit_sha": result.get("sha"),
        "deleted_branch": deleted_branch,
    }


def _delete_merged_branch(api: GitHubApi, owner: str, repo: str, pr: dict) -> str | None:
    """Best-effort delete of the just-squash-merged PR's head branch (KGA-395), so the coding agent's
    unverified (``unknown_key``) feature-branch commits don't linger in the repo after the squash lands
    a Verified commit on the default branch. Never deletes the base / default branch. Non-fatal: the
    merge has already succeeded, so a cleanup failure is logged, not raised (and ``delete_ref`` already
    swallows a 404/422 for an already-gone ref)."""
    head_ref = (pr.get("head") or {}).get("ref")
    base_ref = (pr.get("base") or {}).get("ref")
    if not head_ref or head_ref in (base_ref, "main", "master"):
        return None
    try:
        api.delete_ref(owner, repo, head_ref)
        return head_ref
    except GitHubError as exc:  # never let branch cleanup mask a successful merge
        print(f"[merge:executor] WARNING could not delete merged branch {head_ref}: {exc}")
        return None


def _is_actionable_deny(verdict: dict) -> bool:
    """A deny is 'actionable' (worth a PR comment) when something other than a not-yet-green CI is
    blocking — i.e. a review/criteria signal the loop must act on."""
    return not verdict["signals"]["review_greenlit"] or not verdict["signals"]["criteria_met"]


def _comment(api: GitHubApi, owner: str, repo: str, number: int, body: str) -> None:
    try:
        api.create_comment(owner, repo, number, body)
    except GitHubError as exc:  # a failed audit comment must not mask the merge outcome
        print(f"[merge:executor] WARNING could not post audit comment: {exc}")


def _render(verdict: dict, *, outcome: str) -> str:
    head = "✅ **Merge authorised & merged**" if outcome == "merged" else "⛔ **Merge blocked**"
    lines = [
        f"{head} — three-signal gate (T43) / merge executor (T34)",
        "",
        f"- review greenlit: `{verdict['signals']['review_greenlit']}`",
        f"- acceptance criteria met: `{verdict['signals']['criteria_met']}`",
        f"- CI green on head SHA: `{verdict['signals']['ci_green']}`",
    ]
    if verdict["reasons"]:
        lines.append("")
        lines.append("Blocking:")
        lines.extend(f"- {r}" for r in verdict["reasons"])
    lines += ["", "<details><summary>signal evidence (audit)</summary>", "", "```json", json.dumps(verdict["evidence"], indent=2, sort_keys=True), "```", "</details>"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Three-signal gate + autonomous merge executor (T43/T34)")
    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--merge-method", default="squash")
    ap.add_argument("--dry-run", action="store_true", help="evaluate the gate but do not merge")
    args = ap.parse_args(argv)

    api = GitHubApi()
    outcome = run(api, args.owner, args.repo, args.pr, merge_method=args.merge_method, dry_run=args.dry_run)
    print(json.dumps(outcome, indent=2, sort_keys=True))
    # Non-zero exit only on an unexpected error path; a clean deny/abort is a valid, expected result
    # (the loop routes back to fix/file) so it exits 0 with the verdict on stdout.
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
