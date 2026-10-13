# Daily stale sweep

Daniel reviewed: no

The daily workflow runs at 11:17 UTC after its tests pass. Enable it only after CTO provisions `STALE_SWEEP_READ_TOKEN` with access to `list_projects`, `list_modules`, and `list_work_items`, confirms the append-only `OPS_SERVER_TOKEN`, and sets repository variable `STALE_SWEEP_ENABLED=true`. Manual dispatch also requires main and the enable flag. No credentials are exposed to pull-request jobs.

The sweep reports nonterminal rows unchanged for 14 days as review candidates, missing module locations, and coverage gaps. Paused and deferred work is excluded from age candidates. It expands each project's lists when global responses are capped; any remaining caps are reported. It never closes work or guesses a replacement folder. The dashboard probe reports HTTP status only; authenticated UI freshness remains unverified.

Hosted runners cannot inspect the local vault catalog, folders, or loop states. The worklog explicitly records this gap on every run. CTO must choose a private snapshot export or a local runner before those surfaces can receive daily coverage. Do not copy internal snapshots into this public repository or publish workflow artifacts. A failed request exits nonzero with a sanitized error; GitHub's failed-run status is the delivery-failure record.

For a local census, run `python3 scripts/stale_sweep.py --snapshot /private/path/graph.json --vault-root /path/to/NovaCaelum_Obs --report /private/path/report.json --write-catalog-inventory`. A snapshot contains `list_projects`, `list_modules`, `list_work_items`, and optional `coverage_gaps`. Only the generated `AgentSecretBase/catalog/.stale-sweep-inventory.json` is repaired, with deterministic file hashes and sizes. Authored catalog indexes and release archives require owner review. Add `--post` with the append credential to report results. Without `--snapshot`, a scoped read credential is required.

Validation: `python3 -m unittest test_stale_sweep.py` covers age exclusions, path confinement, generated-inventory idempotence, capped reads, bounded worklog delivery with persisted UUID, and dashboard access gates.

Cleanup rollout dependency: CTO applies `20261012214000` (paused enum) alone and commits it first, then `20261012214335` (dependency trigger), then `20261012214336` (project context/module locations). The ops server needs the new table and column before it starts. This workflow needs no database migration itself; deploy the ops changes before evaluating code-repo coverage. No deployment or secret provisioning is performed by this PR.

## Pending dashboard regeneration

The cleanup follow-up adds daily regeneration with `python3 _agentOS/system/bin/daniel_dashboard.py` after Nova-Caelum/_agentOS#178 merges. The prerequisite is still open as of 2026-10-13 04:03 UTC. No dashboard execution step is enabled by this PR yet.

The script needs the real private vault, local verifier results and runtime deployment manifest, plus the machine's GitHub access. It writes the generated dashboard into that vault. A hosted checkout of this public repository cannot supply those inputs or deliver the output back to the local vault; adding the command there would not regenerate the intended dashboard.

CTO must resolve the existing local-runner versus private-export decision before enablement. With an approved local execution path and the merged canonical script present, run the command from the vault root after the sweep, verify that the generated dashboard timestamp advances, and report its outcome in the same bounded worklog entry. Keep local state and generated output out of public logs and artifacts. Do not attach a privileged local runner to public pull-request jobs.
