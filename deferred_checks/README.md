# Deferred checks registry

One JSON file per Task Graph row whose evidence lands after the work (a scheduled run, a time window).

- `scripts/deferred_checks.py` (this repo, `Deferred checks` workflow, every 3 h) evaluates the `cloud` checks,
  posts one worklog verdict per row (`deferred-check` + `pass`|`fail` + `row-<id>`), and alarms Daniel on Telegram when
  the Mac closer's heartbeat is older than 48 h, a PASS sits unapplied for 24 h, or a row is past `due` + `grace_hours`.
- `_agentOS/system/bin/deferred_check_closer.py` (launchd `com.novacaelum.deferred-check-closer`, hourly) re-verifies
  every check itself, writes `evidence_path` with `evidence_line`, and closes the row through the verifier.

Fields: `row`, `project`, `external_id`, `evaluator` (`cloud`|`mac`), `check` (`github_workflow_run`,
`escalation_rate_drop`, `local_report`, `catalog_covers_repos`, `composite`), `evidence_path` (must sit under
`AgentSecretBase/_ops/deferred-checks/evidence/`), `evidence_line` (the row's file_state criterion expects it), `due`, `grace_hours`.
The row's criterion must be a `file_state` `contains` of `evidence_line` at `evidence_path`, so the verifier, not this
registry, decides done. Data only: no code is read from here.
