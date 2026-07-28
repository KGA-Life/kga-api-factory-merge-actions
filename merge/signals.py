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

DEFAULT_MERGE_CANDIDATE_LABEL = "merge-candidate"
DEFAULT_BOT_LOGIN = "claude[bot]"

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

    ``ignore_check_names`` drops runs whose name CONTAINS any given token (a substring match, not
    exact — e.g. the post-merge-verify workflow's own run, whose check-run name is the job id on the
    dispatch path but may be composed as ``<caller> / post-merge-verify`` on the ``workflow_call``
    path; the substring match catches both, so it doesn't wait on itself). Runs whose ``head_sha``
    is present and != ``sha`` are dropped as stale — the VG-4 guard: a green on a superseded SHA
    never counts.
    """
    relevant = []
    stale_dropped = 0
    for run in check_runs or []:
        name = run.get("name") or ""
        if any(tok in name for tok in ignore_check_names):
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
