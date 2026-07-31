"""T33 — review-outcome routing, the GREENLIGHT arm (KGA-176) + un-greenlight (KGA-337 coordination).

The coding agent already owns the other two arms of T33's fix/file/greenlight router: it fixes small
findings on-branch and re-requests review, and routes larger findings to the hub via
``request_human_assistance`` (which files a Linear issue). This module owns the third arm — turning a
clean ``@claude`` review into the ``merge-candidate`` label the T43 gate consumes — plus its inverse,
removing the label when a later review turns blocking, so the label stays a faithful proxy.

It is deliberately a SEPARATE actor from the coding agent: the agent is barred from mutating labels
(and its config can't yet be updated in place), so the label decision is a deterministic Action here,
reusing the same verdict logic the gate reads (``signals.review_verdict``). One direction of the T33
loop, one place.

THE LOAD-BEARING WIRING (mirrors T47's merge_token requirement): the label must be applied by a REAL
actor (the ``merge_token`` PAT), NOT the default ``github-actions[bot]`` token — GitHub does not fire
workflows (here: ``merge-gate.yml`` on ``pull_request: labeled``) for events caused by the built-in
token. With the PAT, applying ``merge-candidate`` fires the gate; without it the label lands but the
gate never runs (bring-up fallback = manual re-fire). The workflow wires ``GITHUB_TOKEN`` accordingly.

Ordering: the router applies the label only when CI is already green on the head SHA (a bounded poll
absorbs the case where the review verdict lands just before CI finishes), so the gate fires exactly
once on a green head and merges — never the label-before-CI transient the gate documents as a gap.

CLI: ``python -m merge.router --owner O --repo R --pr N [--dry-run]``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import bounds, signals
from .github_api import GitHubApi, GitHubError

MERGE_CANDIDATE_LABEL = os.environ.get("MERGE_CANDIDATE_LABEL", signals.DEFAULT_MERGE_CANDIDATE_LABEL)
CLAUDE_BOT_LOGIN = os.environ.get("CLAUDE_BOT_LOGIN", signals.DEFAULT_BOT_LOGIN)
# T38 (KGA-181) runaway escape hatch: the max PR fix↔review rounds before the loop is deemed
# non-converging. Env-overridable; authoritative default lives in merge.bounds (same MAX_REVIEW_ROUNDS
# knob the relay driver reads). Read at import so a test can monkeypatch this module global.
MAX_REVIEW_ROUNDS = bounds.DEFAULT_MAX_REVIEW_ROUNDS

# CI-green pre-check excludes the same runs the gate excludes: the advisory AI-review runs (KGA-334)
# and the gate's own check-run (KGA-336), so the router's view of "green" matches the gate's.
_IGNORE_CHECK_NAMES = (
    *signals.AI_REVIEW_CHECK_NAMES,
    os.environ.get("MERGE_GATE_CHECK_NAME", "gate-and-merge"),
)
_POLL_ATTEMPTS = int(os.environ.get("ROUTER_POLL_ATTEMPTS", "10"))
_POLL_INTERVAL = int(os.environ.get("ROUTER_POLL_INTERVAL", "15"))

ACTION_APPLY = "apply"
ACTION_REMOVE = "remove"
ACTION_NONE = "none"
ACTION_ESCAPE_HATCH = "escape_hatch"  # T38: review loop exceeded its round budget without converging


def decide_route(
    *,
    verdict: str,
    review_final: bool,
    ci_green: bool,
    labeled: bool,
    label: str = MERGE_CANDIDATE_LABEL,
) -> dict:
    """Pure decision: what to do with the ``merge-candidate`` label given the review verdict and the
    supporting signals. Idempotent — never re-applies a present label or removes an absent one.

      * ``CHANGES_REQUESTED`` -> REMOVE the label if present (un-greenlight; a blocking review must
        not leave a merge-candidate standing). CI is irrelevant to a block.
      * ``APPROVE`` + a final review + CI green + not already labeled -> APPLY (greenlight).
      * everything else -> NONE: no verdict yet, an approve whose review isn't final or whose CI isn't
        green, or a state already consistent with the verdict.
    """
    if verdict == signals.REVIEW_CHANGES_REQUESTED:
        if labeled:
            return {"action": ACTION_REMOVE, "reason": f"review verdict is CHANGES_REQUESTED; removing {label}"}
        return {"action": ACTION_NONE, "reason": "blocking review, label already absent"}
    if verdict == signals.REVIEW_APPROVE:
        if labeled:
            return {"action": ACTION_NONE, "reason": "approving review, label already present"}
        if not review_final:
            return {"action": ACTION_NONE, "reason": "approve verdict but review not yet final"}
        if not ci_green:
            return {"action": ACTION_NONE, "reason": "approve verdict but CI not green on head SHA"}
        return {"action": ACTION_APPLY, "reason": f"clean review (APPROVE) + CI green; applying {label}"}
    return {"action": ACTION_NONE, "reason": "no explicit review verdict yet"}


def _poll_ci_green(api, owner, repo, sha, *, sleep, attempts=_POLL_ATTEMPTS, interval=_POLL_INTERVAL):
    """Bounded poll of CI on ``sha`` until every relevant run has concluded (or ``attempts`` is
    exhausted). Returns ``(green, evidence)``. Mirrors revert._poll_ci but with router knobs — absorbs
    the review-verdict-lands-just-before-CI ordering so a valid PR is greenlit once CI settles, rather
    than left un-labelled. A timeout with runs still pending reports not-green."""
    green, ev = False, {"reason": "no poll performed"}
    for _ in range(max(1, attempts)):
        green, ev = signals.ci_green_for_sha(
            api.list_check_runs(owner, repo, sha),
            api.get_combined_status(owner, repo, sha),
            sha,
            ignore_check_names=_IGNORE_CHECK_NAMES,
        )
        if not ev.get("pending_runs"):
            return green, ev
        sleep(interval)
    return green, ev


def run(
    api: GitHubApi,
    owner: str,
    repo: str,
    number: int,
    *,
    dry_run: bool = False,
    sleep=time.sleep,
) -> dict:
    """Gather the signals for the PR and apply/remove ``merge-candidate`` accordingly. Returns a
    structured outcome dict (always includes ``outcome`` + ``action`` + ``verdict``)."""
    pr = api.get_pull(owner, repo, number)
    if pr.get("merged") or pr.get("state") != "open":
        return {"outcome": "skipped_not_open", "action": ACTION_NONE, "verdict": None, "state": pr.get("state")}

    head_sha = (pr.get("head") or {}).get("sha")
    labeled, label_ev = signals.merge_candidate_labeled(pr.get("labels", []), label_name=MERGE_CANDIDATE_LABEL)
    comments = api.list_issue_comments(owner, repo, number)
    reviews = api.list_reviews(owner, repo, number)
    review_final, review_ev = signals.review_present_and_final(comments, bot_login=CLAUDE_BOT_LOGIN)
    verdict, verdict_ev = signals.review_verdict(
        reviews, comments, bot_login=CLAUDE_BOT_LOGIN, head_sha=head_sha
    )

    # T38 (KGA-181) runaway escape hatch: has the fix↔review cycle blown its round budget WITHOUT
    # converging to a clean approval? Count completed review rounds (bot verdict comments / graded
    # reviews). If over the cap AND the latest verdict is not APPROVE, TRIP — un-greenlight + audit
    # comment — rather than let the loop run unbounded. A slow-but-converged APPROVE (over the cap but
    # now clean) is deliberately let through to the normal greenlight below. Checked before the CI poll:
    # like a block, a runaway trip doesn't care about CI, so don't burn a poll on it.
    rounds_verdict = bounds.review_rounds_exceeded(
        bounds.count_review_rounds(comments, reviews, bot_login=CLAUDE_BOT_LOGIN), MAX_REVIEW_ROUNDS
    )
    if rounds_verdict["exceeded"] and verdict != signals.REVIEW_APPROVE:
        return _escape_hatch(
            api, owner, repo, number,
            labeled=labeled, verdict=verdict, rounds_verdict=rounds_verdict,
            evidence={"label": label_ev, "review": review_ev, "review_verdict": verdict_ev, "head_sha": head_sha},
            dry_run=dry_run,
        )

    # CI only gates the APPLY path; a block (REMOVE) doesn't care about CI, so don't burn a poll on it.
    # Also require review_final: decide_route rejects APPLY when the review isn't final regardless of
    # CI, so polling in that case (e.g. an APPROVE marker in a not-yet-final comment) is wasted work.
    if verdict == signals.REVIEW_APPROVE and not labeled and review_final:
        ci_green, ci_ev = _poll_ci_green(api, owner, repo, head_sha, sleep=sleep)
    else:
        ci_green, ci_ev = False, {"skipped": "CI pre-check only runs for an approve + review-final + unlabelled PR"}

    decision = decide_route(verdict=verdict, review_final=review_final, ci_green=ci_green, labeled=labeled)
    evidence = {
        "label": label_ev,
        "review": review_ev,
        "review_verdict": verdict_ev,
        "ci": ci_ev,
        "head_sha": head_sha,
    }
    result = {"outcome": decision["action"], "action": decision["action"], "verdict": verdict,
              "reason": decision["reason"], "evidence": evidence}

    if dry_run or decision["action"] == ACTION_NONE:
        result["outcome"] = "would_" + decision["action"] if dry_run else decision["action"]
        return result

    if decision["action"] == ACTION_APPLY:
        api.add_labels(owner, repo, number, [MERGE_CANDIDATE_LABEL])
    elif decision["action"] == ACTION_REMOVE:
        api.remove_label(owner, repo, number, MERGE_CANDIDATE_LABEL)
    _comment(api, owner, repo, number, _render(decision, verdict))
    return result


def _escape_hatch(
    api: GitHubApi,
    owner: str,
    repo: str,
    number: int,
    *,
    labeled: bool,
    verdict: str,
    rounds_verdict: dict,
    evidence: dict,
    dry_run: bool,
) -> dict:
    """The T38 runaway escape hatch (KGA-181): the fix↔review loop has exceeded its round budget without
    converging. Withdraw ``merge-candidate`` if present (a runaway PR must not stay a merge candidate)
    and post an audit comment flagging the build for human help rather than looping unbounded. Returns a
    structured ``escape_hatch`` outcome carrying the bound decision — the auditable trip record the
    T39/T51 layer surfaces to Slack (the Slack delivery itself is deferred to T39, soft-coupled)."""
    result = {
        "outcome": ACTION_ESCAPE_HATCH,
        "action": ACTION_ESCAPE_HATCH,
        "verdict": verdict,
        "reason": rounds_verdict["reason"],
        "rounds": rounds_verdict,
        "evidence": evidence,
    }
    if dry_run:
        result["outcome"] = "would_escape_hatch"
        return result
    if labeled:
        api.remove_label(owner, repo, number, MERGE_CANDIDATE_LABEL)
    _comment(api, owner, repo, number, _render_escape_hatch(rounds_verdict, verdict))
    return result


def _render_escape_hatch(rounds_verdict: dict, verdict: str) -> str:
    return (
        f"🛑 **Runaway escape hatch tripped (T38).** The fix↔review cycle has run "
        f"{rounds_verdict['count']} review round(s) — over the cap of {rounds_verdict['cap']} — without "
        f"converging to a clean approval (latest verdict: `{verdict}`). The `{MERGE_CANDIDATE_LABEL}` "
        f"label is withheld/withdrawn and this build is flagged for human help rather than looping "
        f"unbounded. Review will keep tripping the hatch until a human intervenes or the cap is raised."
    )


def _comment(api: GitHubApi, owner: str, repo: str, number: int, body: str) -> None:
    try:
        api.create_comment(owner, repo, number, body)
    except GitHubError as exc:  # an audit comment must never mask the routing outcome
        print(f"[merge:router] WARNING could not post audit comment: {exc}")


def _render(decision: dict, verdict: str) -> str:
    if decision["action"] == ACTION_APPLY:
        return (
            f"✅ **Greenlit — `{MERGE_CANDIDATE_LABEL}` applied (T33).** The `@claude` review verdict is "
            f"`APPROVE` and CI is green on the head SHA, so the PR is handed to the three-signal merge "
            f"gate (T43)."
        )
    return (
        f"↩️ **Un-greenlit — `{MERGE_CANDIDATE_LABEL}` removed (T33).** The latest `@claude` review "
        f"verdict is `REQUEST_CHANGES`, so the merge-candidate label is withdrawn until a clean review "
        f"lands. (verdict: `{verdict}`)"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="T33 review-outcome router: greenlight / un-greenlight the merge-candidate label")
    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true", help="evaluate and log the label action but do not apply it")
    args = ap.parse_args(argv)

    api = GitHubApi()
    outcome = run(api, args.owner, args.repo, args.pr, dry_run=args.dry_run)
    print(json.dumps(outcome, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
