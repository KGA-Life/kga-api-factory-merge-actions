# kga-api-factory-merge-actions

Reusable GitHub Actions + Python for the **KGA API Factory autonomous-merge gate** — the three-layer
defense chain that lets the coding-agent loop merge its own PRs with no human in the inner loop, and
stay safe doing it (M4 · T43 / T34 / T47).

A generated integration repo references the two reusable workflows here by tag; the machinery is
authored, unit-tested, and versioned **once**, centrally, and every repo inherits it. This repo was
extracted from `kga-api-factory-relay` per **KGA-333** so the machinery has a single public,
versioned home (public → callable by any org repo, and the `merge/` checkout is tokenless).

## The chain

```
 T43 gate (decide)          T34 executor (act + re-verify)          T47 net (catch + reverse)
 review ∧ criteria ∧ CI  →  re-read head SHA, re-check CI-green   →  post-merge CI on merge commit;
 deny on any subset         on that exact SHA, then REST-merge       red → idempotent auto-revert
```

`merge/` holds the code (pure `signals` + `gate`, thin `github_api`, `executor`, `revert`); see
[`merge/README.md`](merge/README.md) for the module-level contract and safety properties.

## Reusable workflows

### `merge-gate.yml` — T43 gate + T34 merge executor

Call it from a generated repo on the `merge-candidate` label:

```yaml
# .github/workflows/merge-gate.yml (in the generated repo)
name: merge-gate
on:
  pull_request:
    types: [labeled]
jobs:
  gate:
    if: github.event.label.name == 'merge-candidate'
    uses: KGA-Life/kga-api-factory-merge-actions/.github/workflows/merge-gate.yml@v1
    with:
      pr: ${{ github.event.pull_request.number }}
      # machinery_ref: v1   # optional: pin the checked-out merge/ code to the same tag
    secrets: inherit          # provides GITHUB_TOKEN; pass merge_token for a cross-repo PAT
```

`workflow_dispatch` on this repo fires it manually against an explicit `{owner, repo, pr}` and
defaults to `--dry-run` (evaluate the gate, do not merge).

### `post-merge-verify.yml` — T47 verify + auto-revert

Call it from the generated repo on push to the default branch:

```yaml
# .github/workflows/post-merge-verify.yml (in the generated repo)
name: post-merge-verify
on:
  push:
    branches: [main]
jobs:
  verify:
    uses: KGA-Life/kga-api-factory-merge-actions/.github/workflows/post-merge-verify.yml@v1
    with:
      merge_sha: ${{ github.sha }}
    secrets: inherit
```

## Versioning

Callers pin `@v1` (or a later tag). `merge/` is stdlib-only, so there is nothing to install; the
reusable workflow checks THIS repo out at `machinery_ref` (default `main`) to run the code. For strict
alignment, pass `machinery_ref: v1` so the checked-out code matches the workflow file's tag.

## Local dev

```bash
python -m ruff check merge
python -m pytest              # merge/tests, no network
```
