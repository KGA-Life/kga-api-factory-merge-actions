"""KGA API Factory — the autonomous-merge machinery (M4 · T43/T34/T47).

The three-layer defense chain that lets the coding loop merge its own PRs without a human in
the inner loop, and stay safe while doing it:

  * ``gate``      — T43: the pure three-signal decision (review ∧ criteria ∧ CI-green). Decides
                    WHETHER to merge; never touches the branch.
  * ``executor``  — T34: the non-Claude-Code merge actor. Re-checks CI-green on the EXACT PR
                    head SHA immediately before merging (the load-bearing stale-SHA guard the
                    Free-plan org can't get from branch protection), then merges via REST.
  * ``revert``    — T47: the post-merge net. Re-verifies the default branch on the merge commit
                    and, on red, auto-reverts (idempotently, keyed on the merge SHA) and reopens
                    the issue.

Split like the ``driver`` package: pure functions (``signals``, ``gate``, and the ``decide_*``
helpers) carry the correctness contract and unit-test with no network; ``github_api`` is the thin
stdlib-only (urllib) REST layer the orchestration modules call. Nothing here imports boto3 or any
third-party package — it runs in a GitHub Actions runner with only the standard library.
"""
