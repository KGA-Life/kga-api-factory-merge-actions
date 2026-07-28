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
    def __init__(self, *, pulls, comments=None, checks, statuses, merge_error=None, merge_result=None):
        self._pulls = list(pulls)
        self._comments = comments if comments is not None else _final_comments()
        self._checks = list(checks)
        self._statuses = list(statuses)
        self._merge_error = merge_error
        self._merge_result = merge_result or {"sha": "mergecommitsha"}
        self.created_comments: list[str] = []
        self.merge_calls: list[dict] = []

    @staticmethod
    def _next(seq):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def get_pull(self, o, r, n):
        return self._next(self._pulls)

    def list_issue_comments(self, o, r, n):
        return self._comments

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


# --- fixtures ----------------------------------------------------------------
def _open_pr(sha, *, labeled=True, mergeable=None):
    return {
        "merged": False,
        "state": "open",
        "head": {"sha": sha},
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
    assert api.merge_calls == [{"sha": A, "merge_method": "merge"}]
    assert len(api.created_comments) == 1  # the audit record


# --- the load-bearing re-verify guards --------------------------------------
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
