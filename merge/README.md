# `merge/` — the autonomous-merge machinery (M4 · T43 / T34 / T47)

The three-layer defense chain that lets the coding loop merge its own PRs with no human in the
inner loop, and stay safe doing it. Pure decision logic + a thin stdlib-only GitHub REST layer,
driven by two reusable GitHub Actions workflows. No AWS, no third-party deps.

```
 T43 gate (decide)         T34 executor (act + re-verify)        T47 net (catch + reverse)
 review ∧ criteria ∧ CI  →  re-read head SHA, re-check CI-green  →  post-merge CI on merge commit;
 deny on any miss           on that exact SHA, then REST-merge      red → idempotent auto-revert
```

## Modules

| Module | Kind | Role |
|---|---|---|
| `signals.py` | pure | Derive the gate signals from GitHub payloads. `ci_green_for_sha` is the hard signal and owns the **stale-SHA guard** (a green on any other SHA never counts). `review_present_and_final` reuses the KGA-134 `claude[bot]`-review completion heuristic. `review_verdict` (KGA-337) reads the reviewer's explicit **APPROVE / REQUEST_CHANGES** verdict — a formal review state, or a `VERDICT:` marker in the review comment — with a stale-review guard. |
| `gate.py` | pure | `evaluate_gate` — the three-signal AND. Authorises **only** when all three hold; lists every missing signal otherwise. Decides *whether*, never touches the branch. |
| `router.py` | pure + thin | T33 greenlight / un-greenlight (KGA-176 / KGA-337). `decide_route` maps the review verdict to a label action; `run` applies `merge-candidate` on a clean review (`APPROVE` + CI-green on the head SHA) and removes it on a blocking one (`REQUEST_CHANGES`), so the label the gate consumes stays a faithful proxy of the review outcome. The **producer** of the label the gate is the consumer of. |
| `executor.py` | thin | T34 — the non-Claude-Code merge actor. Gathers signals, evaluates the gate (now including `review_verdict` — a `REQUEST_CHANGES` verdict is a hard block even with the label on), and — the load-bearing bit — **re-reads the head SHA and re-checks CI-green immediately before merging**, refusing on a moved head or non-green re-check. Merges via REST (`merge` method) with the `sha` param as a server-side backstop. |
| `revert.py` | pure + thin | T47 — post-merge verify + auto-revert. Polls CI on the merge commit (excluding its own run), and on red opens+merges a revert PR, **idempotent on the merge SHA**. |
| `github_api.py` | thin I/O | The only module that touches the network (stdlib `urllib`). Injected into `router`/`executor`/`revert` so the logic unit-tests with no network. |

## The contract with the rest of the loop

- **Upstream (T33 routing).** The **`router.py` / `review-router.yml`** step applies the
  **`merge-candidate` label** automatically on a clean `@claude` review (verdict `APPROVE` + CI-green)
  and removes it on a blocking one (`REQUEST_CHANGES`). The gate reads that label as signals (i)+(ii),
  independently confirms a `claude[bot]` review *ran and finished* (defense-in-depth), and **also reads
  the verdict directly and denies on `REQUEST_CHANGES`** (KGA-337), so a blocking review can't be
  merged even if the label is stale. The router applies the label with the `merge_token` PAT so the
  label event actually fires `merge-gate.yml` (a `github-actions[bot]` label write would not).
- **Signal (iii)** is CI-green on the PR head SHA — the one hard, deterministic input, re-checked at
  merge time.
- **Linear transition.** The executor never sets the issue Done — the PR's `Closes KGA-###` keyword
  auto-transitions it on merge (the GitHub↔Linear integration).
- **Non-Claude-Code actor.** Both workflows run as `github-actions[bot]` (or a Claudette PAT on
  dispatch), never a Claude Code session, so the auto-mode self-merge classifier does not apply.

## Workflows

- `.github/workflows/review-router.yml` — T33. Called from the generated repo's `issue_comment`
  (claude[bot] review) events + `workflow_dispatch` (dogfood; defaults to `--dry-run`). Applies /
  removes `merge-candidate` via the `merge_token` PAT so the label event fires `merge-gate.yml`.
- `.github/workflows/merge-gate.yml` — T43+T34. `workflow_call` (the M2 template wires a thin caller
  at T35) + `workflow_dispatch` (dogfood; defaults to `--dry-run`).
- `.github/workflows/post-merge-verify.yml` — T47. Called from the generated repo's push-to-main
  workflow with the merge SHA; `workflow_dispatch` for manual runs.

## Local dev

```bash
python -m ruff check merge
python -m pytest merge/tests -q
# dry-run the gate against a real PR without merging:
GITHUB_TOKEN=… python -m merge.executor --owner KGA-Life --repo <repo> --pr <n> --dry-run
```

## Known gaps (tracked, non-blocking for this PR)

- **Live end-to-end** (a real gated merge + a deliberately-red auto-revert against a generated repo)
  is the **T35 (KGA-178)** acceptance dry-run, not proven here — this PR ships the tested machinery.
- **Linear reopen on revert** is a clean injectable seam (`reopen_issue`). The Action has no Linear
  token yet, so absent a callback it records a "manual Linear reopen needed" annotation and reports
  `skipped_no_token`. Wiring a real Linear callback is a follow-up.
- **Independent criteria re-verify.** Signal (ii) is trusted via the `merge-candidate` label;
  independently re-reading the Linear acceptance criteria at gate time also needs a Linear token.
- **Cross-repo reusable-workflow hosting.** Referenced from a private generated repo, checking out
  this (machinery) repo may need a read-scoped token — the driver for extracting the machinery to a
  public, versioned Actions repo (**KGA-333**, Option 3).
