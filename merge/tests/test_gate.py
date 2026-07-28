"""Unit tests for the pure three-signal gate decision (T43)."""

from __future__ import annotations

from merge import gate


def test_authorise_only_when_all_three():
    v = gate.evaluate_gate(review_greenlit=True, criteria_met=True, ci_green=True)
    assert v["authorise"] is True
    assert v["reasons"] == []


def test_deny_on_missing_review():
    v = gate.evaluate_gate(review_greenlit=False, criteria_met=True, ci_green=True)
    assert v["authorise"] is False
    assert any("review" in r for r in v["reasons"])


def test_deny_on_missing_criteria():
    v = gate.evaluate_gate(review_greenlit=True, criteria_met=False, ci_green=True)
    assert v["authorise"] is False
    assert any("criteria" in r for r in v["reasons"])


def test_deny_on_missing_ci():
    v = gate.evaluate_gate(review_greenlit=True, criteria_met=True, ci_green=False)
    assert v["authorise"] is False
    assert any("CI" in r for r in v["reasons"])


def test_deny_lists_every_missing_signal():
    v = gate.evaluate_gate(review_greenlit=False, criteria_met=False, ci_green=False)
    assert v["authorise"] is False
    assert len(v["reasons"]) == 3


def test_no_merge_on_any_subset():
    # KGA-177 acceptance: the merge never fires on a SUBSET of signals. Exhaustively, every
    # combination that isn't all-three must deny.
    for r in (True, False):
        for c in (True, False):
            for ci in (True, False):
                v = gate.evaluate_gate(review_greenlit=r, criteria_met=c, ci_green=ci)
                assert v["authorise"] is (r and c and ci)


def test_evidence_passthrough_and_signals_recorded():
    v = gate.evaluate_gate(review_greenlit=True, criteria_met=True, ci_green=True, evidence={"ci": {"sha": "x"}})
    assert v["evidence"] == {"ci": {"sha": "x"}}
    assert v["signals"] == {"review_greenlit": True, "criteria_met": True, "ci_green": True}
