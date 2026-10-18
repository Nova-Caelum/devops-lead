# Escalation override report

Daniel reviewed: no

The reviewer's `escalate-daniel` label is a soft gate: a PR can still be merged over it. This job never blocks a merge. After the fact, it sends Daniel one Telegram message per PR that was merged while it carried the label, so he can review.

## The message

One message per PR, plain text:

- `repo#number`, the title, the PR link and the merge-commit link.
- Who merged it and when (UTC).
- **The objection:** the verdict line and the item lines from the reviewer's escalation comment (the latest one). If the comment has no such structure, its `ESCALATION-REASON:` line. If there is no escalation comment, a notice saying so.
- **Why it was needed:** the body of the latest PR comment that starts with `Merged over escalation because:`. If there is none, `no reason given`. The match is on that exact prefix; a comment that merely says the same thing in other words does not count.

## How it works

`.github/workflows/escalation-override-report.yml` runs `scripts/escalation_override_report.py` every 15 minutes from this repository. One org-wide search (`is:pr is:merged label:escalate-daniel`) covers every reviewed repo; no caller repo is edited.

- **Report once.** The mark is the label `escalation-override-reported` on the PR; the search excludes it, and each PR is re-read before it is claimed, so search-index lag cannot double-send. To send a PR again, remove the label.
- **Order is claim, send, give the claim back on failure.** A token that cannot write labels fails on the first claim and sends nothing; it can never loop on the same PRs. If the send fails, the label is removed so the next run retries. If that removal also fails, the run exits non-zero and names the PR (`ROLLBACK_FAILED`).
- **Floor.** Only PRs merged on or after `2026-10-18T00:00:00Z` (`SINCE` in the script) are reported. Older merges already carry the label by the hundred and are not part of this.
- **Settling time.** A PR must have been merged 10 minutes ago, so the merger has time to post the reason and the search index to catch up.
- **Cap.** At most 10 PRs per run, oldest first; the rest wait for the next run.
- **Public logs.** This repository is public. A hosted run prints counts and failure classes only, never a title, a comment or a message. Only `--dry-run` prints a message, and it is for local use.

## Local use

```
python3 scripts/escalation_override_report.py --pr Nova-Caelum/<repo>#<n> --dry-run   # print the message, change nothing
python3 scripts/escalation_override_report.py --pr Nova-Caelum/<repo>#<n>             # send it for real (needs the two Telegram variables)
```

`--pr` ignores the floor and the settling time, never the reported mark. It uses your own `gh` login.

## Secrets and switch

Set on this repository. Nothing runs until `ESCALATION_REPORT_ENABLED` is `true`.

| Name | Kind | What |
|---|---|---|
| `ESCALATION_REPORT_GITHUB_TOKEN` | secret | Fine-grained PAT on the org: **Pull requests read and write** on every reviewed repo (`_agentOS`, `Caelos`, `caelos-website`, `technical-cofounder`, `technical-cofounder-website`, `hyperspace-engine`, `palladia`, `no-mistakes`, `novacaelum-landing`). Private repos must be in the grant or the search silently omits them. |
| `STALE_SWEEP_GITHUB_TOKEN` | secret | Fallback when the above is unset. The stale sweep documents it as read-only, so it covers finding and reading the PRs but not the label; it works here only if widened to pull-requests write. |
| `TELEGRAM_BOT_TOKEN` | secret | The DevOps-Lead bot, the same one the reviewer's escalation notice uses. |
| `TELEGRAM_DANIEL_CHAT_ID` | secret | Daniel's chat. |
| `ESCALATION_REPORT_ENABLED` | variable | `true` to switch the schedule on. |

Scheduled workflows in a public repository are disabled by GitHub after 60 days without repository activity; re-enable from the Actions tab if that happens.

## Validation

`python3 -m unittest test_escalation_override_report.py` covers the objection parser, the reason matcher, message limits, the claim/send/rollback order, report-once across runs and a stale search index, the floor, the settling time, the cap, token-free errors and public-log silence. It does not attest scheduled delivery from Actions until the secrets and the switch are in place.
