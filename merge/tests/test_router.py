"""Unit tests for the T33 review-outcome router (greenlight / un-greenlight the merge-candidate
label). No network: a FakeApi returns programmed payloads and records label writes. The bounded CI
poll runs with a no-op sleep, and green/red checks conclude immediately so no real waiting occurs."""

from __future__ import annotations

from merge import router, signals

A = "aaaaaaaaaaaa1111"
B = "bbbbbbbbbbbb2222"


class FakeApi:
    def __init__(self, *, pull, comments=None, reviews=None, checks=None, statuses=None):
        self._pull = pull
        self._comments = comments if comments is not None else []
        self._reviews = list(reviews or [])
        self._checks = checks if checks is not None else _green(A)
        self._statuses = statuses if statuses is not None else _empty_status()
        self.added: list[list[str]] = []
        self.removed: list[str] = []
        self.comments_posted: list[str] = []

    def get_pull(self, o, r, n):
        return self._pull

    def list_issue_comments(self, o, r, n):
        return self._comments

    def list_reviews(self, o, r, n):
        return self._reviews

    def list_check_runs(self, o, r, sha):
        return self._checks

    def get_combined_status(self, o, r, sha):
        return self._statuses

    def add_labels(self, o, r, n, labels):
        self.added.append(labels)
        return {}

    def remove_label(self, o, r, n, label):
        self.removed.append(label)
        return {}

    def create_comment(self, o, r, n, body):
        self.comments_posted.append(body)
        return {"id": 1}


# --- fixtures ----------------------------------------------------------------
def _open_pr(sha=A, *, labeled=False):
    return {
        "merged": False,
        "state": "open",
        "head": {"sha": sha},
        "labels": [{"name": "merge-candidate"}] if labeled else [{"name": "harness"}],
    }


def _comment(body, login="claude[bot]", cid=1, created="2026-07-28T10:00:00Z"):
    return {"user": {"login": login}, "body": body, "id": cid, "created_at": created}


def _approve_comment():
    return [_comment("Review complete ✅ looks good.\n\nVERDICT: APPROVE")]


def _block_comment():
    return [_comment("Findings remain — needs work.\n\nVERDICT: REQUEST_CHANGES")]


def _green(sha):
    return [{"name": "lint-and-test", "status": "completed", "conclusion": "success", "head_sha": sha}]


def _red(sha):
    return [{"name": "lint-and-test", "status": "completed", "conclusion": "failure", "head_sha": sha}]


def _empty_status():
    return {"state": "pending", "total_count": 0}


def _run(api, dry_run=False):
    return router.run(api, "KGA-Life", "kga-x", 7, dry_run=dry_run, sleep=lambda _s: None)


# --- pure decide_route -------------------------------------------------------
def test_decide_block_removes_when_labeled():
    d = router.decide_route(verdict=signals.REVIEW_CHANGES_REQUESTED, review_final=True, ci_green=True, labeled=True)
    assert d["action"] == router.ACTION_REMOVE


def test_decide_block_noop_when_unlabeled():
    d = router.decide_route(verdict=signals.REVIEW_CHANGES_REQUESTED, review_final=True, ci_green=True, labeled=False)
    assert d["action"] == router.ACTION_NONE


def test_decide_approve_applies_when_clean_and_unlabeled():
    d = router.decide_route(verdict=signals.REVIEW_APPROVE, review_final=True, ci_green=True, labeled=False)
    assert d["action"] == router.ACTION_APPLY


def test_decide_approve_noop_when_already_labeled():
    d = router.decide_route(verdict=signals.REVIEW_APPROVE, review_final=True, ci_green=True, labeled=True)
    assert d["action"] == router.ACTION_NONE


def test_decide_approve_noop_when_review_not_final():
    d = router.decide_route(verdict=signals.REVIEW_APPROVE, review_final=False, ci_green=True, labeled=False)
    assert d["action"] == router.ACTION_NONE


def test_decide_approve_noop_when_ci_not_green():
    d = router.decide_route(verdict=signals.REVIEW_APPROVE, review_final=True, ci_green=False, labeled=False)
    assert d["action"] == router.ACTION_NONE


def test_decide_no_verdict_is_noop():
    d = router.decide_route(verdict=signals.REVIEW_NONE, review_final=True, ci_green=True, labeled=False)
    assert d["action"] == router.ACTION_NONE


# --- run() integration -------------------------------------------------------
def test_run_greenlights_clean_review():
    api = FakeApi(pull=_open_pr(labeled=False), comments=_approve_comment(), checks=_green(A))
    out = _run(api)
    assert out["action"] == router.ACTION_APPLY
    assert api.added == [["merge-candidate"]]
    assert api.removed == []
    assert len(api.comments_posted) == 1


def test_run_ungreenlights_blocking_review():
    api = FakeApi(pull=_open_pr(labeled=True), comments=_block_comment(), checks=_green(A))
    out = _run(api)
    assert out["action"] == router.ACTION_REMOVE
    assert api.removed == ["merge-candidate"]
    assert api.added == []
    assert len(api.comments_posted) == 1


def test_run_blocking_but_unlabeled_is_noop():
    api = FakeApi(pull=_open_pr(labeled=False), comments=_block_comment(), checks=_green(A))
    out = _run(api)
    assert out["action"] == router.ACTION_NONE
    assert api.added == [] and api.removed == [] and api.comments_posted == []


def test_run_approve_already_labeled_is_noop():
    api = FakeApi(pull=_open_pr(labeled=True), comments=_approve_comment(), checks=_green(A))
    out = _run(api)
    assert out["action"] == router.ACTION_NONE
    assert api.added == []


def test_run_no_verdict_is_noop():
    api = FakeApi(pull=_open_pr(labeled=False), comments=[_comment("Review complete, looks good.")], checks=_green(A))
    out = _run(api)
    assert out["action"] == router.ACTION_NONE
    assert api.added == []


def test_run_approve_ci_red_does_not_greenlight():
    # a red head must not be greenlit — the poll concludes immediately (no pending) with green=False.
    api = FakeApi(pull=_open_pr(labeled=False), comments=_approve_comment(), checks=_red(A))
    out = _run(api)
    assert out["action"] == router.ACTION_NONE
    assert api.added == []


def test_run_ai_review_check_does_not_block_greenlight():
    # a failing claude-review check must not veto the greenlight — only the real CI counts (KGA-334).
    mixed = [
        _green(A)[0],
        {"name": "claude-review", "status": "completed", "conclusion": "failure", "head_sha": A},
    ]
    api = FakeApi(pull=_open_pr(labeled=False), comments=_approve_comment(), checks=mixed)
    out = _run(api)
    assert out["action"] == router.ACTION_APPLY
    assert api.added == [["merge-candidate"]]


def test_run_dry_run_does_not_apply():
    api = FakeApi(pull=_open_pr(labeled=False), comments=_approve_comment(), checks=_green(A))
    out = _run(api, dry_run=True)
    assert out["outcome"] == "would_apply"
    assert api.added == [] and api.comments_posted == []


def test_run_stale_block_on_old_commit_does_not_ungreenlight():
    # a CHANGES_REQUESTED FORMAL review bound to an old commit is stale for the current head (VG-4):
    # with an approving marker on the current head, the PR is greenlit, not un-greenlit.
    reviews = [{"user": {"login": "claude[bot]"}, "state": "CHANGES_REQUESTED", "id": 1,
                "submitted_at": "2026-07-28T09:00:00Z", "commit_id": B}]
    api = FakeApi(pull=_open_pr(labeled=False), comments=_approve_comment(), reviews=reviews, checks=_green(A))
    out = _run(api)
    assert out["action"] == router.ACTION_APPLY


def test_run_skips_closed_pr():
    api = FakeApi(pull={"merged": False, "state": "closed", "head": {"sha": A}}, comments=_approve_comment())
    out = _run(api)
    assert out["outcome"] == "skipped_not_open"
    assert api.added == []


def test_main_prints_outcome(monkeypatch, capsys):
    api = FakeApi(pull=_open_pr(labeled=False), comments=_approve_comment(), checks=_green(A))
    monkeypatch.setattr(router, "GitHubApi", lambda *a, **k: api)
    rc = router.main(["--owner", "KGA-Life", "--repo", "kga-x", "--pr", "7", "--dry-run"])
    assert rc == 0
    assert "would_apply" in capsys.readouterr().out
