"""Cost & iteration bounds for the merge machinery — the review-rounds cap (M5 / T38, KGA-181).

Bound the PR fix↔review cycle so a non-converging PR can't loop forever. A **review round** is one
completed reviewer verdict: a ``claude[bot]`` issue comment carrying a ``VERDICT:`` marker (the as-built
@claude Assistant reviews via comments, not formal reviews), or a graded formal review. The round count
is the **max** of the two per-source counts — a single round emits at most one of each, so summing would
double-count; ``max`` mirrors ``driver/bounds.py`` in ``kga-api-factory-relay`` (kept a separate copy —
this public Actions repo cannot import from that private repo, and vice versa).

The "is this a review round?" predicates are the SAME ones the gate's verdict logic uses
(``signals.is_bot_verdict_comment`` / ``signals.is_bot_graded_review``), so the round counter and the
verdict never diverge on what counts as a bot review.

The router (``merge/router.py``) reasons over ``review_rounds_exceeded``: when the cap is blown and the
PR still hasn't converged to a clean approval, the T38 runaway escape hatch trips — un-greenlight
(withdraw ``merge-candidate``) + an audit comment — rather than letting the fix↔review loop run
unbounded.

Config (env; non-secret): ``MAX_REVIEW_ROUNDS`` — a conservative default of 5, tuned once real runs
produce data. Authoritative in the relay's ``lib/driver-config.ts`` for the driver side; this repo reads
the same env name so a caller workflow can override it consistently.

Pure + stdlib-only (no network, no third-party deps): every function takes already-fetched GitHub
payloads, matching the ``signals`` discipline.

DORMANT-ASSUMPTION NOTE (mirrors the relay): ``max`` UNDERCOUNTS on DISJOINT rounds (a round that
emitted only a comment plus a distinct round that emitted only a formal review → ``max(1,1) == 1`` for 2
real rounds). It is safe today because the as-built emits comment verdicts only (``formal_rounds == 0``,
so ``max`` == the comment count); revisit when formal-review submission is wired up.
"""

from __future__ import annotations

import os

from . import signals

# Conservative starting cap; env-overridable. Same env name the relay driver reads (single knob).
DEFAULT_MAX_REVIEW_ROUNDS = int(os.environ.get("MAX_REVIEW_ROUNDS", "5"))


def count_review_rounds(
    comments: list[dict],
    reviews: list[dict] | None = None,
    *,
    bot_login: str = signals.DEFAULT_BOT_LOGIN,
) -> int:
    """Count completed review rounds on a PR from its history. Only the bot's own verdict comments /
    graded reviews count (the agent's re-request comments do not). Returns ``max`` of the two per-source
    counts — see the module docstring for why ``max`` and not the sum."""
    comment_rounds = sum(1 for c in comments or [] if signals.is_bot_verdict_comment(c, bot_login=bot_login))
    formal_rounds = sum(1 for r in reviews or [] if signals.is_bot_graded_review(r, bot_login=bot_login))
    return max(comment_rounds, formal_rounds)


def review_rounds_exceeded(count: int, cap: int) -> dict:
    """Has the PR had more review rounds than allowed? ``exceeded`` iff ``count > cap`` (hitting exactly
    ``cap`` is still in-bounds). Returns a JSON-able bound verdict with a logged reason — the auditable
    record the escape hatch trips on."""
    exceeded = count > cap
    reason = (
        f"{count} review round(s) exceed the max of {cap} — the fix↔review cycle is not converging"
        if exceeded
        else f"{count} review round(s) within the max of {cap}"
    )
    return {"bound": "max_review_rounds", "exceeded": exceeded, "count": count, "cap": cap, "reason": reason}
