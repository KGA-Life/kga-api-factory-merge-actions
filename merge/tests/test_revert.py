"""Unit tests for post-merge verify + auto-revert (T47). No network, no git: the git step is an
injected callable and the API is a fake."""

from __future__ import annotations

from merge import revert
from merge.github_api import GitHubError

MERGE_SHA = "deadbeefcafe0000"
BRANCH = revert.revert_branch_name(MERGE_SHA)  # revert/deadbeefcafe


def _noop_sleep(_):
    pass


class FakeApi:
    def __init__(self, *, checks, statuses=None, pulls=None, revert_pr=None, merge_error=None):
        self._checks = list(checks)
        self._statuses = list(statuses or [{"state": "pending", "total_count": 0}])
        self._pulls = pulls or []
        self._revert_pr = revert_pr or {"number": 99, "head": {"sha": "revsha"}}
        self._merge_error = merge_error
        self.created_pulls: list[str] = []
        self.merge_calls: list = []
        self.deleted_refs: list[str] = []
        self.comments: list = []

    @staticmethod
    def _next(seq):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def list_check_runs(self, o, r, sha):
        return self._next(self._checks)

    def get_combined_status(self, o, r, sha):
        return self._next(self._statuses)

    def list_pulls(self, o, r, state="open", head=None):
        self.list_pulls_head = head
        return self._pulls

    def create_pull(self, o, r, *, title, head, base, body):
        self.created_pulls.append(head)
        return self._revert_pr

    def merge_pull(self, o, r, n, *, sha, merge_method="merge"):
        if self._merge_error:
            raise self._merge_error
        self.merge_calls.append((n, sha, merge_method))
        return {"sha": "revmergecommit"}

    def delete_ref(self, o, r, ref):
        self.deleted_refs.append(ref)
        return {}

    def create_comment(self, o, r, n, body):
        self.comments.append((n, body))
        return {}


def _green_runs():
    return [{"name": "test", "status": "completed", "conclusion": "success", "head_sha": MERGE_SHA}]


def _red_runs():
    return [{"name": "test", "status": "completed", "conclusion": "failure", "head_sha": MERGE_SHA}]


def _pending_runs():
    return [{"name": "test", "status": "in_progress", "conclusion": None, "head_sha": MERGE_SHA}]


# --- pure decide_revert ------------------------------------------------------
def test_decide_green_stands():
    assert revert.decide_revert(ci_green=True, existing_revert=False, merge_sha=MERGE_SHA)["action"] == "none"


def test_decide_existing_revert_is_noop():
    d = revert.decide_revert(ci_green=False, existing_revert=True, merge_sha=MERGE_SHA)
    assert d["action"] == "none"
    assert "already exists" in d["reason"]


def test_decide_red_reverts():
    assert revert.decide_revert(ci_green=False, existing_revert=False, merge_sha=MERGE_SHA)["action"] == "revert"


def test_revert_branch_name_is_merge_sha_keyed():
    assert BRANCH == "revert/deadbeefcafe"


# --- _poll_ci ----------------------------------------------------------------
def test_poll_returns_green_immediately():
    api = FakeApi(checks=[_green_runs()])
    green, _ = revert._poll_ci(api, "o", "r", MERGE_SHA, ignore=(), sleep=_noop_sleep, attempts=3, interval=0)
    assert green is True


def test_poll_waits_through_pending_then_green():
    api = FakeApi(checks=[_pending_runs(), _green_runs()])
    green, _ = revert._poll_ci(api, "o", "r", MERGE_SHA, ignore=(), sleep=_noop_sleep, attempts=3, interval=0)
    assert green is True


def test_poll_timeout_with_pending_is_not_green():
    api = FakeApi(checks=[_pending_runs()])
    green, ev = revert._poll_ci(api, "o", "r", MERGE_SHA, ignore=(), sleep=_noop_sleep, attempts=2, interval=0)
    assert green is False
    assert ev["pending_runs"]


def test_poll_excludes_own_run_by_name_substring():
    # the verify workflow's own (still-running) check must not make the poll wait on itself; the
    # real run is green. Name-substring exclusion (the composed workflow_call name form).
    own = {"name": "caller / post-merge-verify", "status": "in_progress", "conclusion": None, "head_sha": MERGE_SHA}
    api = FakeApi(checks=[[own, _green_runs()[0]]])
    green, ev = revert._poll_ci(api, "o", "r", MERGE_SHA, ignore=(revert.VERIFY_CHECK_NAME,), sleep=_noop_sleep, attempts=2, interval=0)
    assert green is True
    assert not ev["pending_runs"]


def test_poll_excludes_own_run_by_run_id():
    # name-independent self-exclusion: the run whose details_url carries this Actions run id is
    # dropped even if its name doesn't match the ignore token at all.
    own = {"name": "verify-and-maybe-revert", "status": "in_progress", "conclusion": None,
           "head_sha": MERGE_SHA, "details_url": "https://github.com/o/r/actions/runs/9999/job/1"}
    api = FakeApi(checks=[[own, _green_runs()[0]]])
    green, ev = revert._poll_ci(api, "o", "r", MERGE_SHA, ignore=(), sleep=_noop_sleep, self_run_id="9999", attempts=2, interval=0)
    assert green is True
    assert not ev["pending_runs"]


# --- _existing_revert --------------------------------------------------------
def test_existing_revert_detects_branch():
    api = FakeApi(checks=[_red_runs()], pulls=[{"head": {"ref": BRANCH}}])
    assert revert._existing_revert(api, "o", "r", BRANCH) is True


def test_existing_revert_absent():
    api = FakeApi(checks=[_red_runs()], pulls=[{"head": {"ref": "feat/other"}}])
    assert revert._existing_revert(api, "o", "r", BRANCH) is False


# --- run ---------------------------------------------------------------------
def test_run_green_stands_no_git_no_pr():
    api = FakeApi(checks=[_green_runs()])
    calls = []
    out = revert.run(
        api, "KGA-Life", "kga-x", merge_sha=MERGE_SHA, pr_number=7, issue_id="KGA-204",
        git_revert_and_push=lambda *a: calls.append(a), sleep=_noop_sleep,
    )
    assert out["outcome"] == "stood"
    assert calls == [] and api.created_pulls == [] and api.merge_calls == []


def test_run_red_with_existing_revert_is_idempotent_noop():
    api = FakeApi(checks=[_red_runs()], pulls=[{"head": {"ref": BRANCH}}])
    calls = []
    out = revert.run(
        api, "KGA-Life", "kga-x", merge_sha=MERGE_SHA,
        git_revert_and_push=lambda *a: calls.append(a), sleep=_noop_sleep,
    )
    assert out["outcome"] == "noop_existing_revert"
    assert calls == [] and api.created_pulls == []


def test_run_red_reverts_and_merges_and_annotates():
    api = FakeApi(checks=[_red_runs()])
    calls = []
    out = revert.run(
        api, "KGA-Life", "kga-x", merge_sha=MERGE_SHA, pr_number=7, issue_id="KGA-204",
        git_revert_and_push=lambda *a: calls.append(a), sleep=_noop_sleep,
    )
    assert out["outcome"] == "reverted"
    assert out["revert_pr"] == 99
    assert calls == [(MERGE_SHA, BRANCH, "main")]         # git revert on the merge-sha-keyed branch
    assert api.created_pulls == [BRANCH]                   # revert PR opened from that branch
    assert api.merge_calls == [(99, "revsha", "squash")]  # revert PR squash-merged (KGA-395)
    assert api.deleted_refs == [BRANCH]                   # ...and its branch cleaned up afterwards
    assert out["linear_reopen"] == "skipped_no_token"      # no Linear callback wired
    assert api.comments and api.comments[0][0] == 7        # original PR annotated


def test_run_red_revert_merge_refused_is_structured_not_raised():
    # KGA-395 follow-up: if GitHub refuses the revert PR's squash-merge (e.g. 405 squash disabled),
    # the run must NOT raise — it surfaces a structured `revert_merge_refused` outcome. Otherwise the
    # revert PR is left open-but-unmerged and `_existing_revert` no-ops every later run, silently
    # leaving main red. The branch is NOT deleted on a failed merge (the PR stays for a human).
    api = FakeApi(checks=[_red_runs()], merge_error=GitHubError(405, "Merge not allowed"))
    out = revert.run(
        api, "KGA-Life", "kga-x", merge_sha=MERGE_SHA, pr_number=7, issue_id="KGA-204",
        git_revert_and_push=lambda *a: None, sleep=_noop_sleep,
    )
    assert out["outcome"] == "revert_merge_refused"
    assert out["status"] == 405
    assert out["revert_pr"] == 99
    assert api.created_pulls == [BRANCH]  # the revert PR WAS opened...
    assert api.deleted_refs == []         # ...but its branch is not deleted on a failed merge


def test_run_red_reopen_callback_invoked_when_provided():
    api = FakeApi(checks=[_red_runs()])
    reopened = []
    out = revert.run(
        api, "KGA-Life", "kga-x", merge_sha=MERGE_SHA, pr_number=7, issue_id="KGA-204",
        git_revert_and_push=lambda *a: None,
        reopen_issue=lambda issue, reason: reopened.append((issue, reason)),
        sleep=_noop_sleep,
    )
    assert out["linear_reopen"] == "reopened"
    assert reopened and reopened[0][0] == "KGA-204"


def test_run_ignores_failing_ai_review_and_stands():
    # merge-commit CI: real test green + a FAILED claude-review must NOT trigger a revert (KGA-334) —
    # the poll ignores AI-review runs, so a healthy merge stands.
    mixed = [
        {"name": "test", "status": "completed", "conclusion": "success", "head_sha": MERGE_SHA},
        {"name": "claude-review", "status": "completed", "conclusion": "failure", "head_sha": MERGE_SHA},
    ]
    api = FakeApi(checks=[mixed])
    calls = []
    out = revert.run(
        api, "KGA-Life", "kga-x", merge_sha=MERGE_SHA,
        git_revert_and_push=lambda *a: calls.append(a), sleep=_noop_sleep,
    )
    assert out["outcome"] == "stood"
    assert calls == []
