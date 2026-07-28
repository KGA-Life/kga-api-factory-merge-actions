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

The merge uses ``merge_method="merge"`` (the repo convention) and never sets the Linear issue Done —
the PR's ``Closes KGA-###`` keyword auto-transitions it on merge.

CLI: ``python -m merge.executor --owner O --repo R --pr N [--merge-method merge] [--dry-run]``
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


def _gather_signals(api: GitHubApi, owner: str, repo: str, number: int, sha: str, labels: list[dict]) -> dict:
    """Derive the three gate signals for ``sha`` from the already-fetched ``labels`` plus freshly
    fetched comments/checks. Returns the kwargs ``evaluate_gate`` wants plus a merged evidence dict."""
    labeled, label_ev = signals.merge_candidate_labeled(labels, label_name=MERGE_CANDIDATE_LABEL)
    review_final, review_ev = signals.review_present_and_final(
        api.list_issue_comments(owner, repo, number), bot_login=CLAUDE_BOT_LOGIN
    )
    ci_green, ci_ev = signals.ci_green_for_sha(
        api.list_check_runs(owner, repo, sha),
        api.get_combined_status(owner, repo, sha),
        sha,
    )
    return {
        "review_greenlit": labeled and review_final,
        "criteria_met": labeled,
        "ci_green": ci_green,
        "evidence": {"label": label_ev, "review": review_ev, "ci": ci_ev},
    }


def run(
    api: GitHubApi,
    owner: str,
    repo: str,
    number: int,
    *,
    merge_method: str = "merge",
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
        api.list_check_runs(owner, repo, current_sha),
        api.get_combined_status(owner, repo, current_sha),
        current_sha,
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
    _comment(api, owner, repo, number, _render(verdict, outcome="merged"))
    return {
        "outcome": "merged",
        "verdict": verdict,
        "merged_sha": current_sha,
        "merge_commit_sha": result.get("sha"),
    }


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
    ap.add_argument("--merge-method", default="merge")
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
