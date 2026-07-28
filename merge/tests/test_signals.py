"""Unit tests for the pure signal derivations (T43 signals). No network."""

from __future__ import annotations

from merge import signals

SHA = "abc123def456"


# --- merge_candidate_labeled -------------------------------------------------
def test_label_present():
    ok, ev = signals.merge_candidate_labeled([{"name": "merge-candidate"}, {"name": "harness"}])
    assert ok is True
    assert ev["present"] is True


def test_label_absent():
    ok, ev = signals.merge_candidate_labeled([{"name": "harness"}])
    assert ok is False
    assert ev["present"] is False


def test_label_empty_list():
    ok, _ = signals.merge_candidate_labeled([])
    assert ok is False


def test_label_custom_name():
    ok, _ = signals.merge_candidate_labeled([{"name": "ready"}], label_name="ready")
    assert ok is True


# --- review_present_and_final ------------------------------------------------
def _c(login, body, cid=1, created="2026-07-28T10:00:00Z"):
    return {"user": {"login": login}, "body": body, "id": cid, "created_at": created}


def test_review_none_present_is_not_final():
    ok, ev = signals.review_present_and_final([_c("someone-else", "looks good")])
    assert ok is False
    assert "no claude[bot]" in ev["reason"]


def test_review_working_placeholder_is_not_final():
    ok, ev = signals.review_present_and_final([_c("claude[bot]", "Claude Code is working… I'll get back to you")])
    assert ok is False
    assert ev["is_working_placeholder"] is True


def test_review_in_progress_placeholder_is_not_final():
    ok, ev = signals.review_present_and_final([_c("claude[bot]", "Review in progress\n- [ ] correctness")])
    assert ok is False


def test_review_pending_checkbox_is_not_final():
    ok, ev = signals.review_present_and_final([_c("claude[bot]", "Findings:\n- [ ] fix the null deref\n- [x] style")])
    assert ok is False
    assert ev["has_pending_checkbox"] is True


def test_review_final_clean_is_final():
    ok, ev = signals.review_present_and_final([_c("claude[bot]", "Review complete ✅ — all checks pass, good to merge\n- [x] correctness")])
    assert ok is True
    assert ev["final"] is True


def test_review_latest_comment_wins():
    # an early working-placeholder, then a later final verdict -> final (latest by created_at).
    comments = [
        _c("claude[bot]", "Claude Code is working…", cid=1, created="2026-07-28T10:00:00Z"),
        _c("claude[bot]", "Review complete ✅ good to merge", cid=2, created="2026-07-28T10:05:00Z"),
    ]
    ok, ev = signals.review_present_and_final(comments)
    assert ok is True
    assert ev["comment_id"] == 2


def test_review_custom_bot_login():
    ok, _ = signals.review_present_and_final([_c("kga-bot", "good to merge")], bot_login="kga-bot")
    assert ok is True


# --- ci_green_for_sha --------------------------------------------------------
def _run(name, status="completed", conclusion="success", head_sha=SHA):
    return {"name": name, "status": status, "conclusion": conclusion, "head_sha": head_sha}


def _status(state="success", total=0):
    return {"state": state, "total_count": total}


def test_ci_green_all_success():
    green, ev = signals.ci_green_for_sha([_run("build"), _run("test")], _status(), SHA)
    assert green is True
    assert ev["relevant_run_count"] == 2


def test_ci_not_green_pending_run():
    green, ev = signals.ci_green_for_sha([_run("build"), _run("test", status="in_progress", conclusion=None)], _status(), SHA)
    assert green is False
    assert "test" in ev["pending_runs"]


def test_ci_not_green_failed_run():
    green, ev = signals.ci_green_for_sha([_run("build", conclusion="failure")], _status(), SHA)
    assert green is False
    assert "build" in ev["failed_runs"]


def test_ci_not_green_absent_ci():
    green, ev = signals.ci_green_for_sha([], _status(), SHA)
    assert green is False
    assert "absent CI" in ev["reason"]


def test_ci_neutral_and_skipped_count_as_ok():
    green, _ = signals.ci_green_for_sha([_run("a", conclusion="neutral"), _run("b", conclusion="skipped")], _status(), SHA)
    assert green is True


def test_ci_stale_run_on_other_sha_is_dropped():
    # a green run recorded against a DIFFERENT sha must not count (VG-4). With only stale runs and
    # no relevant run, CI is 'absent' for this sha -> not green.
    green, ev = signals.ci_green_for_sha([_run("build", head_sha="oldsha")], _status(), SHA)
    assert green is False
    assert ev["stale_runs_dropped"] == 1
    assert ev["relevant_run_count"] == 0


def test_ci_ignore_check_names_excludes_own_run():
    # the post-merge-verify run on the merge commit is excluded; the remaining real run is green.
    green, _ = signals.ci_green_for_sha(
        [_run("post-merge-verify", status="in_progress", conclusion=None), _run("test")],
        _status(),
        SHA,
        ignore_check_names=("post-merge-verify",),
    )
    assert green is True


def test_ci_ignore_matches_composed_name_substring():
    # on the workflow_call path the self run's name may be composed (e.g. "<caller> /
    # post-merge-verify"); the substring match must still exclude it.
    green, _ = signals.ci_green_for_sha(
        [_run("build / post-merge-verify", status="in_progress", conclusion=None), _run("test")],
        _status(),
        SHA,
        ignore_check_names=("post-merge-verify",),
    )
    assert green is True


def test_ci_combined_status_failure_blocks():
    green, _ = signals.ci_green_for_sha([_run("build")], _status(state="failure", total=1), SHA)
    assert green is False


def test_ci_combined_status_success_with_statuses_passes():
    green, _ = signals.ci_green_for_sha([_run("build")], _status(state="success", total=2), SHA)
    assert green is True
