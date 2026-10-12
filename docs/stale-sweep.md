# Daily stale sweep

Daniel reviewed: no

The daily workflow runs at 11:17 UTC after its tests pass. Enable it only after CTO provisions `STALE_SWEEP_READ_TOKEN` with access to `list_projects`, `list_modules`, and `list_work_items`, confirms the append-only `OPS_SERVER_TOKEN`, and sets repository variable `STALE_SWEEP_ENABLED=true`. Manual dispatch also requires main and the enable flag. No credentials are exposed to pull-request jobs.

The sweep reports nonterminal rows unchanged for 14 days as review candidates, missing module locations, and coverage gaps. Paused and deferred work is excluded from age candidates. It expands each project's lists when global responses are capped; any remaining caps are reported. It never closes work or guesses a replacement folder. The dashboard probe reports HTTP status only; authenticated UI freshness remains unverified.

Hosted runners cannot inspect the local vault catalog, folders, or loop states. The worklog explicitly records this gap on every run. CTO must choose a private snapshot export or a local runner before those surfaces can receive daily coverage. Do not copy internal snapshots into this public repository or publish workflow artifacts. A failed request exits nonzero with a sanitized error; GitHub's failed-run status is the delivery-failure record.

For a local census, run `python3 scripts/stale_sweep.py --snapshot /private/path/graph.json --vault-root /path/to/NovaCaelum_Obs --report /private/path/report.json --write-catalog-inventory`. A snapshot contains `list_projects`, `list_modules`, `list_work_items`, and optional `coverage_gaps`. Only the generated `AgentSecretBase/catalog/.stale-sweep-inventory.json` is repaired, with deterministic file hashes and sizes. Authored catalog indexes and release archives require owner review. Add `--post` with the append credential to report results. Without `--snapshot`, a scoped read credential is required.

Validation: `python3 -m unittest test_stale_sweep.py` covers age exclusions, path confinement, generated-inventory idempotence, capped reads, bounded worklog delivery with persisted UUID, and dashboard access gates.

Cleanup rollout dependency: CTO applies `20261012214000` (paused enum) alone and commits it first, then `20261012214335` (dependency trigger), then `20261012214336` (project context/module locations). The ops server needs the new table and column before it starts. This workflow needs no database migration itself; deploy the ops changes before evaluating code-repo coverage. No deployment or secret provisioning is performed by this PR.
