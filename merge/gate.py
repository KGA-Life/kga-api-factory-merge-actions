"""T43 — the three-signal merge gate, as a pure decision over the three booleans ``signals`` derives.

The whole gate is one AND with a deny-on-any-miss rule and a recorded rationale. It decides WHETHER
to merge and nothing else — it never touches the branch (that is the executor's job, T34), and it
independently re-verifies nothing (the executor re-checks the hard signal at merge time). Keeping
the decision pure and side-effect-free is what makes the "no merge on any subset of signals"
acceptance criterion (KGA-186 / KGA-177) directly unit-testable.
"""

from __future__ import annotations


def evaluate_gate(
    *,
    review_greenlit: bool,
    criteria_met: bool,
    ci_green: bool,
    evidence: dict | None = None,
) -> dict:
    """Return the gate verdict.

    ``{"authorise": bool, "reasons": [<blocking reason>, ...], "signals": {...}, "evidence": {...}}``

    ``authorise`` is ``True`` iff ALL THREE signals hold. ``reasons`` lists every missing signal
    (so the routed-back-to-fix/file comment can name exactly what to fix), and is empty on
    authorise. ``evidence`` is passed straight through to be recorded on the PR for audit.
    """
    reasons: list[str] = []
    if not review_greenlit:
        reasons.append("review not greenlit (no clean, completed @claude review / merge-candidate)")
    if not criteria_met:
        reasons.append("acceptance criteria not marked met (merge-candidate label absent)")
    if not ci_green:
        reasons.append("CI not green on the PR head SHA")

    return {
        "authorise": not reasons,
        "reasons": reasons,
        "signals": {
            "review_greenlit": review_greenlit,
            "criteria_met": criteria_met,
            "ci_green": ci_green,
        },
        "evidence": evidence or {},
    }
