"""Unit tests for the merge executor (T43 gate evaluation + T34 merge w/ head-SHA re-check).

No network: a FakeApi returns programmed payloads. Call-ordered reads (get_pull / list_check_runs /
get_combined_status) consume a sequence, last value sticky — this models the TEMPORAL sequence the
executor depends on (gather signals, then re-read head + re-check CI immediately before merge).
"""

from __future__ import annotations

from merge import executor
from merge.github_api import GitHubError

A = "aaaaaaaaaaaa1111"
B = "bbbbbbbbbbbb2222"


class FakeApi:
    def __init__(self, *, pulls, comments=None, checks, statuses, reviews=None, merge_error=None, merge_result=None, delete_error=None):
        self._pulls = list(pulls)
        self._comments = comments if comments is not None else _final_comments()
        self._checks = list(checks)
        self._statuses = list(statuses)
        self._reviews = list(reviews or [])
        self._merge_error = merge_error
        self._delete_error = delete_error
        self._merge_result = merge_result or {"sha": "mergecommitsha"}
        self.created_comments: list[str] = []
        self.merge_calls: list[dict] = []
        self.deleted_refs: list[str] = []

    @staticmethod
    def _next(seq):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def get_pull(self, o, r, n):
        return self._next(self._pulls)

    def list_issue_comments(self, o, r, n):
        return self._comments

    def list_reviews(self, o, r, n):
        return self._reviews

    def list_check_runs(self, o, r, sha):
        return self._next(self._checks)

    def get_combined_status(self, o, r, sha):
        return self._next(self._statuses)

    def create_comment(self, o, r, n, body):
        self.created_comments.append(body)
        return {"id": 1}

    def merge_pull(self, o, r, n, *, sha, merge_method="merge"):
        if self._merge_error:
            raise self._merge_error
        self.merge_calls.append({"sha": sha, "merge_method": merge_method})
        return self._merge_result

    def delete_ref(self, o, r, ref):
        if self._delete_error:
            raise self._delete_error
        self.deleted_refs.append(ref)
        return {}


# --- fixtures ----------------------------------------------------------------
def _open_pr(sha, *, labeled=True, mergeable=None, head_ref="feat/kga-x-7"):
    return {
        "merged": False,
        "state": "open",
        "head": {"sha": sha, "ref": head_ref},
        "base": {"ref": "main"},
        "labels": [{"name": "merge-candidate"}] if labeled else [{"name": "harness"}],
        "mergeable": mergeable,
    }


def _final_comments():
    return [{"user": {"login": "claude[bot]"}, "body": "Review complete ✅ good to merge", "id": 1, "created_at": "2026-07-28T10:00:00Z"}]


def _green(sha):
    return [{"name": "test", "status": "completed", "conclusion": "success", "head_sha": sha}]


def _pending(sha):
    return [{"name": "test", "status": "in_progress", "conclusion": None, "head_sha": sha}]


def _empty_status():
    return {"state": "pending", "total_count": 0}


def _run(api, dry_run=False):
    return executor.run(api, "KGA-Life", "kga-x", 7, dry_run=dry_run)


# --- state guards ------------------------------------------------------------
def test_already_merged_is_noop():
    api = FakeApi(pulls=[{"merged": True, "state": "closed"}], checks=[[]], statuses=[_empty_status()])
    assert _run(api)["outcome"] == "already_merged"
    assert api.merge_calls == []


def test_closed_pr_not_open():
    api = FakeApi(pulls=[{"merged": False, "state": "closed", "head": {"sha": A}}], checks=[[]], statuses=[_empty_status()])
    assert _run(api)["outcome"] == "not_open"


# --- deny paths --------------------------------------------------------------
def test_deny_missing_label_is_actionable_and_comments():
    api = FakeApi(pulls=[_open_pr(A, labeled=False)], checks=[_green(A)], statuses=[_empty_status()])
    out = _run(api)
    assert out["outcome"] == "denied"
    assert out["verdict"]["authorise"] is False
    assert len(api.created_comments) == 1  # actionable deny -> a routing comment
    assert api.merge_calls == []


def test_deny_ci_only_is_transient_no_comment():
    # label present + review final, but CI still pending -> deny, but NOT commented (transient).
    api = FakeApi(pulls=[_open_pr(A)], checks=[_pending(A)], statuses=[_empty_status()])
    out = _run(api)
    assert out["outcome"] == "denied"
    assert api.created_comments == []
    assert api.merge_calls == []


# --- dry run -----------------------------------------------------------------
def test_dry_run_authorised_does_not_merge():
    api = FakeApi(pulls=[_open_pr(A)], checks=[_green(A)], statuses=[_empty_status()])
    out = _run(api, dry_run=True)
    assert out["outcome"] == "would_merge"
    assert api.merge_calls == []
    assert api.created_comments == []


# --- happy merge -------------------------------------------------------------
def test_authorised_stable_head_merges_on_current_sha():
    api = FakeApi(pulls=[_open_pr(A), _open_pr(A)], checks=[_green(A), _green(A)], statuses=[_empty_status(), _empty_status()])
    out = _run(api)
    assert out["outcome"] == "merged"
    assert out["merged_sha"] == A
    assert out["merge_commit_sha"] == "mergecommitsha"
    assert api.merge_calls == [{"sha": A, "merge_method": "squash"}]
    assert api.deleted_refs == ["feat/kga-x-7"]  # KGA-395: the merged branch is cleaned up
    assert out["deleted_branch"] == "feat/kga-x-7"
    assert len(api.created_comments) == 1  # the audit record


# --- the load-bearing re-verify guards --------------------------------------
# --- KGA-395: squash + post-merge branch cleanup --------------------------------------------------
def test_merged_branch_is_deleted_after_squash():
    # the squash merge lands a Verified commit on main; the head branch (carrying the agent's
    # unverified commits) is then deleted so nothing unverified lingers.
    api = FakeApi(pulls=[_open_pr(A, head_ref="feat/KGA-204"), _open_pr(A, head_ref="feat/KGA-204")],
                  checks=[_green(A), _green(A)], statuses=[_empty_status(), _empty_status()])
    out = _run(api)
    assert out["outcome"] == "merged"
    assert api.merge_calls == [{"sha": A, "merge_method": "squash"}]
    assert api.deleted_refs == ["feat/KGA-204"]
    assert out["deleted_branch"] == "feat/KGA-204"


def test_base_branch_is_never_deleted():
    # defensive: a PR whose head ref is the base branch itself must never trigger a base-branch delete.
    api = FakeApi(pulls=[_open_pr(A, head_ref="main"), _open_pr(A, head_ref="main")],
                  checks=[_green(A), _green(A)], statuses=[_empty_status(), _empty_status()])
    out = _run(api)
    assert out["outcome"] == "merged"
    assert api.deleted_refs == []
    assert out["deleted_branch"] is None


def test_branch_delete_failure_does_not_fail_the_merge():
    # a failed branch delete is best-effort: it is swallowed, the merge outcome stands, and
    # deleted_branch is None (cleanup never masks a successful merge).
    api = FakeApi(pulls=[_open_pr(A), _open_pr(A)], checks=[_green(A), _green(A)],
                  statuses=[_empty_status(), _empty_status()],
                  delete_error=GitHubError(403, "insufficient permission to delete ref"))
    out = _run(api)
    assert out["outcome"] == "merged"
    assert out["deleted_branch"] is None
    assert api.deleted_refs == []  # the delete raised before recording


def test_head_advanced_between_gate_and_merge_aborts_stale():
    # gather saw sha A (green); the re-read sees sha B -> stale, refuse (VG-4).
    api = FakeApi(pulls=[_open_pr(A), _open_pr(B)], checks=[_green(A)], statuses=[_empty_status()])
    out = _run(api)
    assert out["outcome"] == "aborted_stale"
    assert api.merge_calls == []
    assert any("advanced" in r for r in out["verdict"]["reasons"])


def test_unmergeable_conflicts_aborts():
    api = FakeApi(pulls=[_open_pr(A), _open_pr(A, mergeable=False)], checks=[_green(A), _green(A)], statuses=[_empty_status(), _empty_status()])
    out = _run(api)
    assert out["outcome"] == "aborted_unmergeable"
    assert api.merge_calls == []


def test_ci_went_red_at_recheck_aborts():
    # green at gather, red at the immediately-before-merge re-check -> refuse.
    api = FakeApi(pulls=[_open_pr(A), _open_pr(A)], checks=[_green(A), _pending(A)], statuses=[_empty_status(), _empty_status()])
    out = _run(api)
    assert out["outcome"] == "aborted_ci_recheck"
    assert api.merge_calls == []


def test_merge_call_refused_by_github():
    api = FakeApi(
        pulls=[_open_pr(A), _open_pr(A)],
        checks=[_green(A), _green(A)],
        statuses=[_empty_status(), _empty_status()],
        merge_error=GitHubError(409, "Head branch was modified. Review and try the merge again."),
    )
    out = _run(api)
    assert out["outcome"] == "merge_refused"
    assert out["status"] == 409


def test_main_prints_outcome(monkeypatch, capsys):
    api = FakeApi(pulls=[_open_pr(A)], checks=[_green(A)], statuses=[_empty_status()])
    monkeypatch.setattr(executor, "GitHubApi", lambda *a, **k: api)
    rc = executor.main(["--owner", "KGA-Life", "--repo", "kga-x", "--pr", "7", "--dry-run"])
    assert rc == 0
    assert "would_merge" in capsys.readouterr().out


def test_failing_ai_review_does_not_block_merge():
    # a FAILED claude-review check alongside a green lint-and-test must not block the gate (KGA-334):
    # the executor passes AI_REVIEW_CHECK_NAMES to ci_green_for_sha, so only the real CI counts.
    mixed = [
        _green(A)[0],
        {"name": "claude-review", "status": "completed", "conclusion": "failure", "head_sha": A},
    ]
    api = FakeApi(pulls=[_open_pr(A), _open_pr(A)], checks=[mixed, mixed], statuses=[_empty_status(), _empty_status()])
    out = _run(api)
    assert out["outcome"] == "merged"
    assert api.merge_calls == [{"sha": A, "merge_method": "squash"}]


# --- KGA-336: the executor must not count its OWN merge-gate check-run as pending CI -------------
def test_own_merge_gate_check_ignored_by_name_merges():
    # the merge-gate workflow's own in-progress check-run (`merge-gate / gate-and-merge`) sits on the
    # PR head SHA while the executor runs; it must be excluded (leaf name 'gate-and-merge') or the
    # gate reads its own run as pending CI and denies every merge (the self-reference deadlock).
    mixed = [
        _green(A)[0],
        {"name": "merge-gate / gate-and-merge", "status": "in_progress", "conclusion": None, "head_sha": A},
    ]
    api = FakeApi(pulls=[_open_pr(A), _open_pr(A)], checks=[mixed, mixed], statuses=[_empty_status(), _empty_status()])
    out = _run(api)
    assert out["outcome"] == "merged"
    assert api.merge_calls == [{"sha": A, "merge_method": "squash"}]


def test_own_run_dropped_by_run_id_merges(monkeypatch):
    # name-independent self-exclusion: a still-running check whose details_url carries THIS Actions
    # run id is dropped even when its leaf name is not in the ignore list — the robust fix (KGA-336).
    monkeypatch.setattr(executor, "SELF_RUN_ID", "42424242")
    mixed = [
        _green(A)[0],
        {"name": "renamed-gate-job", "status": "in_progress", "conclusion": None, "head_sha": A,
         "details_url": "https://github.com/KGA-Life/kga-x/actions/runs/42424242/job/9"},
    ]
    api = FakeApi(pulls=[_open_pr(A), _open_pr(A)], checks=[mixed, mixed], statuses=[_empty_status(), _empty_status()])
    out = _run(api)
    assert out["outcome"] == "merged"
    assert api.merge_calls == [{"sha": A, "merge_method": "squash"}]


def test_unrelated_pending_check_still_blocks():
    # guardrail against over-exclusion: a genuinely-different in-progress check (not this workflow's
    # own run, name not ignored) must STILL block the merge as a transient CI-not-green deny.
    mixed = [
        _green(A)[0],
        {"name": "integration-tests", "status": "in_progress", "conclusion": None, "head_sha": A},
    ]
    api = FakeApi(pulls=[_open_pr(A)], checks=[mixed], statuses=[_empty_status()])
    out = _run(api)
    assert out["outcome"] == "denied"
    assert api.merge_calls == []


# --- KGA-337: the gate must BLOCK on a changes-requested review verdict -------------------------
def _review(state, login="claude[bot]", rid=1, submitted="2026-07-28T10:00:00Z", commit=A):
    return {"user": {"login": login}, "state": state, "id": rid, "submitted_at": submitted, "commit_id": commit}


def test_blocking_formal_review_denies_even_with_label_and_green_ci():
    # label present, review comment final, CI green — but a formal CHANGES_REQUESTED review from the
    # bot must block the merge (the hole T35 surfaced). Actionable deny -> a routing comment.
    api = FakeApi(pulls=[_open_pr(A)], checks=[_green(A)], statuses=[_empty_status()],
                  reviews=[_review("CHANGES_REQUESTED")])
    out = _run(api)
    assert out["outcome"] == "denied"
    assert out["verdict"]["signals"]["review_greenlit"] is False
    assert any("CHANGES_REQUESTED" in r for r in out["verdict"]["reasons"])
    assert api.merge_calls == []
    assert len(api.created_comments) == 1  # actionable -> commented


def test_blocking_verdict_marker_in_comment_denies():
    # no formal review, but the latest claude[bot] comment carries a VERDICT marker -> block.
    marker = [{"user": {"login": "claude[bot]"}, "body": "Findings remain.\n\nVERDICT: REQUEST_CHANGES",
               "id": 9, "created_at": "2026-07-28T11:00:00Z"}]
    api = FakeApi(pulls=[_open_pr(A)], comments=marker, checks=[_green(A)], statuses=[_empty_status()])
    out = _run(api)
    assert out["outcome"] == "denied"
    assert api.merge_calls == []


def test_approving_formal_review_merges():
    # an explicit APPROVED review is not a block; with label + green CI it merges.
    api = FakeApi(pulls=[_open_pr(A), _open_pr(A)], checks=[_green(A), _green(A)],
                  statuses=[_empty_status(), _empty_status()], reviews=[_review("APPROVED")])
    out = _run(api)
    assert out["outcome"] == "merged"
    assert api.merge_calls == [{"sha": A, "merge_method": "squash"}]


def test_no_verdict_is_non_regressive_and_merges():
    # the current reality (reviewer emits neither a formal review nor a marker) must behave exactly
    # as before: label + final review comment + green CI -> merged.
    api = FakeApi(pulls=[_open_pr(A), _open_pr(A)], checks=[_green(A), _green(A)],
                  statuses=[_empty_status(), _empty_status()], reviews=[])
    out = _run(api)
    assert out["outcome"] == "merged"
    assert out["verdict"]["evidence"]["review_verdict"]["verdict"] == "none"
    assert api.merge_calls == [{"sha": A, "merge_method": "squash"}]


def test_stale_blocking_review_on_old_commit_does_not_block():
    # a CHANGES_REQUESTED review bound to an OLD commit must not block the current head (VG-4 for
    # reviews): label + final comment + green CI on head A merge despite the stale block on B.
    api = FakeApi(pulls=[_open_pr(A), _open_pr(A)], checks=[_green(A), _green(A)],
                  statuses=[_empty_status(), _empty_status()],
                  reviews=[_review("CHANGES_REQUESTED", commit=B)])
    out = _run(api)
    assert out["outcome"] == "merged"
    assert api.merge_calls == [{"sha": A, "merge_method": "squash"}]


def test_later_approval_overrides_earlier_changes_requested():
    # the LATEST graded review wins: an earlier CHANGES_REQUESTED followed by a later APPROVED merges.
    api = FakeApi(
        pulls=[_open_pr(A), _open_pr(A)], checks=[_green(A), _green(A)],
        statuses=[_empty_status(), _empty_status()],
        reviews=[_review("CHANGES_REQUESTED", rid=1, submitted="2026-07-28T10:00:00Z"),
                 _review("APPROVED", rid=2, submitted="2026-07-28T10:30:00Z")],
    )
    out = _run(api)
    assert out["outcome"] == "merged"
