"""Unit tests for the M5 / T38 review-rounds bound (KGA-181). No network: pure counters/decisions over
already-fetched GitHub comment/review payloads. Fixtures mirror the shapes the signals verdict
predicates read — a claude[bot] comment carrying a VERDICT marker, and a graded formal review."""

from __future__ import annotations

from merge import bounds


def _comments() -> list[dict]:
    return [
        {"user": {"login": "claude[bot]"}, "body": "Findings remain. VERDICT: REQUEST_CHANGES"},   # round
        {"user": {"login": "claude[bot]"}, "body": "Claude Code is working, no verdict yet"},       # no marker
        {"user": {"login": "keaton-claude-kga"}, "body": "@claude re-review VERDICT: APPROVE"},      # not the bot
        {"user": {"login": "claude[bot]"}, "body": "LGTM. VERDICT: APPROVE"},                        # round
    ]


def _reviews() -> list[dict]:
    return [
        {"user": {"login": "claude[bot]"}, "state": "CHANGES_REQUESTED"},  # round
        {"user": {"login": "claude[bot]"}, "state": "COMMENTED"},          # not graded
        {"user": {"login": "someone"}, "state": "APPROVED"},               # not the bot
    ]


# --- count_review_rounds -----------------------------------------------------
def test_count_from_verdict_comments():
    assert bounds.count_review_rounds(_comments()) == 2


def test_count_is_max_of_sources_not_sum():
    # 2 comment verdicts + 1 graded review -> max(2, 1) == 2 (summing would double-count to 3).
    assert bounds.count_review_rounds(_comments(), _reviews()) == 2


def test_count_formal_reviews_dominate():
    comments = [{"user": {"login": "claude[bot]"}, "body": "VERDICT: APPROVE"}]
    reviews = [
        {"user": {"login": "claude[bot]"}, "state": "CHANGES_REQUESTED"},
        {"user": {"login": "claude[bot]"}, "state": "CHANGES_REQUESTED"},
        {"user": {"login": "claude[bot]"}, "state": "APPROVED"},
    ]
    assert bounds.count_review_rounds(comments, reviews) == 3


def test_count_ignores_non_bot_and_unmarked():
    noise = [
        {"user": {"login": "octocat"}, "body": "VERDICT: APPROVE"},
        {"user": {"login": "claude[bot]"}, "body": "no marker here"},
    ]
    assert bounds.count_review_rounds(noise) == 0


def test_count_empty():
    assert bounds.count_review_rounds([]) == 0
    assert bounds.count_review_rounds(None, None) == 0


def test_count_honours_custom_bot_login():
    comments = [{"user": {"login": "reviewer[bot]"}, "body": "VERDICT: APPROVE"}]
    assert bounds.count_review_rounds(comments) == 0  # default bot login doesn't match
    assert bounds.count_review_rounds(comments, bot_login="reviewer[bot]") == 1


# --- review_rounds_exceeded --------------------------------------------------
def test_within_cap_at_boundary():
    v = bounds.review_rounds_exceeded(5, 5)  # exceeded iff count > cap, so 5 > 5 is within
    assert v["exceeded"] is False and v["bound"] == "max_review_rounds"
    assert "within" in v["reason"]


def test_over_cap():
    v = bounds.review_rounds_exceeded(6, 5)
    assert v["exceeded"] is True and v["count"] == 6 and v["cap"] == 5
    assert "not converging" in v["reason"]
