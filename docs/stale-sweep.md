# Daily stale sweep

Daniel reviewed: no

CTO split the job on 2026-10-13: hosted GitHub Actions checks the ops API and GitHub; the canonical daily local launchd job owns vault pointers, loop states, catalog inventory and dashboard regeneration. Neither schedule requires a privileged self-hosted GitHub runner.

The hosted workflow runs daily at 11:17 UTC after its tests pass. Enable only after CTO provisions `STALE_SWEEP_READ_TOKEN` for `list_projects`, `list_modules`, `list_work_items`, confirms append-only `OPS_SERVER_TOKEN`, and adds `STALE_SWEEP_GITHUB_TOKEN` with read access to the configured repositories (contents, metadata, pull requests, checks, commit statuses and actions). Set repository variable `STALE_SWEEP_ENABLED=true`. Manual dispatch also requires main and the flag. Pull-request jobs receive no credentials.

`stale_sweep_repos.json` explicitly lists ten repositories and three published marketplace catalogs. Coverage changes are reviewed with this file. The sweep reports nonterminal Task Graph rows unchanged for 14 days (excluding paused/deferred work), missing location metadata, old open PRs and failed PR checks. For each workflow represented in the latest 100 default-branch runs it reports the newest failed run; older failures superseded by success are ignored. PR and run caps are recorded, never presented as exhaustive coverage.

Catalog checks resolve relative plugin sources at the catalog branch and git-subdir plugin manifests at their advertised 40-character SHA. Pins whose subtree changed on the source default branch are owner-review candidates, not automatic upgrades. Compare responses with 300 files are marked truncated. Unsupported catalog source shapes are unverified. Public catalog refs do not establish currency of historical vault archives or internal skills; these are separate lineages.

Task Graph reads expand per project and report any remaining response caps. GitHub failures preserve partial results and make the job fail after worklog delivery. Ops read or delivery failures also exit nonzero. Errors in public CI logs contain exception classes only. Reports remain in runner temporary storage and are never uploaded as public artifacts. The job never reads local vault state, probes the dashboard website, closes work, changes pins or rewrites authored release content.

The companion canonical `system/bin/local_stale_sweep.py` and `com.novacaelum.local-stale-sweep.plist` run daily at 07:17 local time, using the vault-archive installation convention. They regenerate the dashboard after its prerequisite PR 178 merges. Installation and local acceptance are documented beside that job. Hosted and local worklog entries have different tags.

Validation: `python3 -m unittest test_stale_sweep.py` covers age exclusions, no local access, capped reads, bounded delivery with persisted UUID, latest-workflow selection, GitHub failures, and immutable/relative catalog references. It does not attest scheduled delivery before credentials and schedules are enabled.

Cleanup migration rollout remains CTO-owned: `20261012214000` enum alone and committed, then `20261012214335`, then `20261012214336`; the server needs its new table and column before restart. This sweep itself has no migration. No deployments or secrets are provisioned by this PR.
