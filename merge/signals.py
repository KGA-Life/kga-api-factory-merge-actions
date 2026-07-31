"""Pure signal derivations for the three-signal merge gate (T43). No network — every function
takes already-fetched GitHub payloads and returns ``(bool, evidence)``. ``evidence`` is a small
JSON-able dict recorded on the PR for audit (a T43/T34 acceptance requirement).

The three signals, and how each is sourced:

  (i)   review greenlit    — SOFT. Two parts, both required:
                              * the ``merge-candidate`` label, applied by the T33 routing step
                                ONLY on a clean @claude review (``merge_candidate_labeled``); and
                              * an independent defense-in-depth check that a claude[bot] review
                                actually ran and FINISHED on this PR (``review_present_and_final``),
                                so a label slapped on without a real completed review can't pass.
  (ii)  acceptance criteria — SOFT. Encoded by the same ``merge-candidate`` label: the T33 router
                              reads the Linear acceptance criteria and only labels when they're met.
                              (Independently re-reading criteria from Linear needs a Linear token
                              the Action isn't provisioned with yet — tracked as a hardening gap.)
  (iii) CI-green on head SHA — HARD, deterministic. ``ci_green_for_sha`` — the one signal that is a
                              boolean fact about a specific commit, not an AI judgment.

The stale-SHA correctness property (VG-4) lives in ``ci_green_for_sha``: it is keyed strictly on
the SHA it is asked about; a green result recorded against any other SHA is irrelevant.
"""

from __future__ import annotations

import re

DEFAULT_MERGE_CANDIDATE_LABEL = "merge-candidate"
DEFAULT_BOT_LOGIN = "claude[bot]"

# The reviewer's explicit verdict on the PR (KGA-337). The gate treats CHANGES_REQUESTED as a hard
# block, so a blocking @claude review can't be merged just because CI is green and the label is on.
REVIEW_APPROVE = "approve"
REVIEW_CHANGES_REQUESTED = "changes_requested"
REVIEW_NONE = "none"

# A machine-readable verdict marker a comment-only reviewer can emit (the @claude Assistant posts an
# issue comment, not a formal review), honored as a fallback when no formal review state exists.
_VERDICT_MARKER = re.compile(r"VERDICT:\s*(APPROVE|REQUEST_CHANGES|CHANGES_REQUESTED)", re.IGNORECASE)
# Formal GitHub review states that actually carry a verdict (COMMENTED/DISMISSED/PENDING do not).
_GRADED_STATES = {"APPROVED", "CHANGES_REQUESTED"}

# The @claude PR Assistant edits ONE comment through several shapes before its verdict is final;
# these phrases mark the not-yet-final placeholders (verified on KGA-134, documented in CLAUDE.md).
# A naive "no unchecked boxes" test alone false-positives on the first placeholder (which has NO
# checkboxes at all), so the placeholder-phrase test is a required conjunct, not a nicety.
_WORKING_PLACEHOLDERS = (
    "claude code is working",
    "get back to you",
    "review in progress",
)

# check-run conclusions that count as "not failing" (a green or a deliberately-neutral outcome).
_OK_CONCLUSIONS = {"success", "neutral", "skipped"}

# AI-review workflows post their OWN check-runs — `claude` (the @claude Assistant, claude.yml) and
# `claude-review` (the auto-review, claude-code-review.yml). They are advisory and can fail or stall
# independently of the code (the auto-review currently fails on every PR — KGA-335), so they must
# NOT gate the deterministic CI-green signal: otherwise a red/pending review blocks a valid merge,
# and the post-merge auto-revert would fire on a healthy merge. These are the FULL check-run names
# (matched by leaf + case-insensitively — see ci_green_for_sha), NOT open substrings, so a generated
# repo whose own repo/workflow/job name merely contains "claude" is not swept up. Callers pass this
# to ``ci_green_for_sha`` via ``ignore_check_names``. (KGA-334)
AI_REVIEW_CHECK_NAMES = ("claude", "claude-review")


def merge_candidate_labeled(
    labels: list[dict], *, label_name: str = DEFAULT_MERGE_CANDIDATE_LABEL
) -> tuple[bool, dict]:
    """Signal (ii) + half of (i): the T33 router's declarative assertion that the review was clean
    AND the Linear acceptance criteria are met."""
    names = [lbl.get("name") for lbl in labels or []]
    present = label_name in names
    return present, {"label": label_name, "present": present, "labels": names}


def review_present_and_final(
    comments: list[dict], *, bot_login: str = DEFAULT_BOT_LOGIN
) -> tuple[bool, dict]:
    """Defense-in-depth half of signal (i): confirm a claude[bot] review actually ran and reached a
    FINAL verdict on this PR — not still a "working…"/"in progress" placeholder, and with no pending
    ``- [ ]`` checklist boxes. Uses the robust conjunction from CLAUDE.md's KGA-134 analysis.

    Note: the review is bound to the current code by the T32/§2.3 discipline of re-requesting the
    @claude review on EVERY push (so the latest claude[bot] comment tracks the latest push). The
    hard, SHA-keyed binding lives in signal (iii); this check is existence + completion only, and
    deliberately does NOT parse approve-vs-changes (too fragile) — that judgment is the label's.
    """
    mine = [c for c in comments or [] if (c.get("user") or {}).get("login") == bot_login]
    if not mine:
        return False, {"reason": "no claude[bot] review comment found", "bot_login": bot_login}
    latest = max(mine, key=lambda c: (c.get("created_at") or "", c.get("id") or 0))
    body = (latest.get("body") or "").lower()
    has_pending_box = "- [ ]" in body
    is_placeholder = any(p in body for p in _WORKING_PLACEHOLDERS)
    final = not has_pending_box and not is_placeholder
    return final, {
        "comment_id": latest.get("id"),
        "final": final,
        "has_pending_checkbox": has_pending_box,
        "is_working_placeholder": is_placeholder,
    }


def review_verdict(
    reviews: list[dict],
    comments: list[dict],
    *,
    bot_login: str = DEFAULT_BOT_LOGIN,
    head_sha: str | None = None,
) -> tuple[str, dict]:
    """The reviewer's explicit verdict on the PR (KGA-337) — the piece that lets the gate BLOCK on a
    changes-requested review instead of trusting the label as the only review proxy.

    Two sources, in priority order:
      1. The latest FORMAL GitHub review by ``bot_login`` — authoritative. Only ``APPROVED`` /
         ``CHANGES_REQUESTED`` states carry a verdict (``COMMENTED`` / ``DISMISSED`` / ``PENDING`` do
         not and are skipped).
      2. Fallback (no graded formal review): a ``VERDICT: APPROVE|REQUEST_CHANGES`` marker in the
         latest ``bot_login`` issue comment — so the comment-only @claude Assistant can still express
         a blocking verdict without submitting a formal review.

    Returns ``(verdict, evidence)`` with ``verdict`` one of ``REVIEW_APPROVE`` /
    ``REVIEW_CHANGES_REQUESTED`` / ``REVIEW_NONE``. ``REVIEW_NONE`` (no explicit verdict from either
    source) is deliberately NON-blocking on its own — the gate falls back to its other signals — so
    adding this signal never regresses a repo whose reviewer emits neither a formal review nor a
    marker; it only ever ADDS the ability to catch an explicit block.
    """
    # Shares the graded-review definition with the review-rounds bound (``merge.bounds``) via the one
    # ``is_bot_graded_review`` predicate, so "a graded bot review" means the same thing to the verdict
    # and to the round counter.
    graded = [r for r in reviews or [] if is_bot_graded_review(r, bot_login=bot_login)]

    # VG-4-style staleness guard: a formal review is bound to a commit (``commit_id``); once head
    # moves past it, it no longer speaks to the current code. When a ``head_sha`` is supplied, drop
    # graded reviews recorded against a DIFFERENT commit — mirroring ``ci_green_for_sha``. This stops
    # a stale CHANGES_REQUESTED against an old commit from blocking forever (which formal reviews,
    # outranking the marker, otherwise would): the loop re-requests review on every push, so a fresh
    # review on the new head is expected. A review with no ``commit_id`` is kept (can't prove stale).
    stale_dropped = 0
    if head_sha:
        kept = []
        for r in graded:
            cid = r.get("commit_id")
            if cid and cid != head_sha:
                stale_dropped += 1
            else:
                kept.append(r)
        graded = kept

    if graded:
        latest = max(graded, key=lambda r: (r.get("submitted_at") or "", r.get("id") or 0))
        state = (latest.get("state") or "").upper()
        verdict = REVIEW_CHANGES_REQUESTED if state == "CHANGES_REQUESTED" else REVIEW_APPROVE
        return verdict, {"verdict": verdict, "source": "formal_review", "review_id": latest.get("id"), "state": state, "stale_reviews_dropped": stale_dropped}

    bot_comments = [c for c in comments or [] if (c.get("user") or {}).get("login") == bot_login]
    if bot_comments:
        latest_c = max(bot_comments, key=lambda c: (c.get("created_at") or "", c.get("id") or 0))
        match = _VERDICT_MARKER.search(latest_c.get("body") or "")
        if match:
            token = match.group(1).upper()
            verdict = REVIEW_APPROVE if token == "APPROVE" else REVIEW_CHANGES_REQUESTED
            return verdict, {"verdict": verdict, "source": "comment_marker", "comment_id": latest_c.get("id"), "marker": token, "stale_reviews_dropped": stale_dropped}

    return REVIEW_NONE, {"verdict": REVIEW_NONE, "source": "none", "stale_reviews_dropped": stale_dropped}


def is_bot_verdict_comment(comment: dict, *, bot_login: str = DEFAULT_BOT_LOGIN) -> bool:
    """True iff ``comment`` is a ``bot_login`` issue comment carrying a ``VERDICT:`` marker — i.e. one
    completed review round from the comment-only @claude Assistant. The single source of truth for
    "this comment is a review verdict", reused by the review-rounds bound (``merge.bounds``) so the
    round counter and the gate's verdict logic never diverge."""
    return (comment.get("user") or {}).get("login") == bot_login and bool(
        _VERDICT_MARKER.search(comment.get("body") or "")
    )


def is_bot_graded_review(review: dict, *, bot_login: str = DEFAULT_BOT_LOGIN) -> bool:
    """True iff ``review`` is a GRADED (``APPROVED`` / ``CHANGES_REQUESTED``) formal review by
    ``bot_login`` — a completed review round via the formal-review path (``COMMENTED`` / ``DISMISSED`` /
    ``PENDING`` do not carry a verdict and are excluded). Companion to ``is_bot_verdict_comment``."""
    return (review.get("user") or {}).get("login") == bot_login and (
        review.get("state") or ""
    ).upper() in _GRADED_STATES


def ci_green_for_sha(
    check_runs: list[dict],
    combined_status: dict,
    sha: str,
    *,
    ignore_check_names: tuple[str, ...] = (),
) -> tuple[bool, dict]:
    """Signal (iii), the HARD one. Green iff, for exactly ``sha``:

      * at least one relevant check-run exists (absent CI is NOT green — T43 treats "non-green or
        absent" as a block), and
      * every relevant check-run has ``status == completed`` with a non-failing conclusion, and
      * the legacy combined commit-status, IF any statuses exist, is ``success`` (Actions report as
        check-runs so this is usually empty; when empty it is not consulted).

    ``ignore_check_names`` drops runs by matching each run's **leaf name** — the segment after any
    ``<caller> /`` prefix that a reusable ``workflow_call`` composes onto the job name — **exactly**
    (case-insensitively) against the given full names (e.g. ``post-merge-verify`` for the workflow's
    own run, and the AI-review runs ``AI_REVIEW_CHECK_NAMES``). Leaf-exact (NOT an open substring),
    so a caller whose repo/workflow/job name merely *contains* a token like "claude" is not swept
    up — only a check whose leaf IS ``claude`` / ``claude-review`` is dropped. Runs whose ``head_sha``
    is present and != ``sha`` are dropped as stale — the VG-4 guard: a green on a superseded SHA
    never counts.
    """
    ignore_leaves = {tok.strip().lower() for tok in ignore_check_names}
    relevant = []
    stale_dropped = 0
    for run in check_runs or []:
        name = run.get("name") or ""
        leaf = name.rsplit("/", 1)[-1].strip().lower()
        if leaf in ignore_leaves:
            continue
        run_sha = run.get("head_sha")
        if run_sha and run_sha != sha:
            stale_dropped += 1
            continue
        relevant.append(run)

    pending = [r.get("name") for r in relevant if r.get("status") != "completed"]
    failed = [
        r.get("name")
        for r in relevant
        if r.get("status") == "completed" and r.get("conclusion") not in _OK_CONCLUSIONS
    ]

    status_state = (combined_status or {}).get("state")
    status_total = (combined_status or {}).get("total_count", 0)
    status_ok = status_total == 0 or status_state == "success"

    green = bool(relevant) and not pending and not failed and status_ok
    evidence = {
        "sha": sha,
        "green": green,
        "relevant_run_count": len(relevant),
        "pending_runs": pending,
        "failed_runs": failed,
        "stale_runs_dropped": stale_dropped,
        "combined_status_state": status_state,
        "combined_status_total": status_total,
    }
    if not relevant:
        evidence["reason"] = "no CI check-runs found for this SHA (absent CI is not green)"
    return green, evidence
