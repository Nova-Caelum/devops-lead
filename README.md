# devops-lead cloud-launch repo

Per-agent cloud-launch repo for the **DevOps-Lead** persona (Nova Caelum).

## Purpose

This repo exists so Daniel can open it in Claude Code (mobile / cloud UI) and get a session running the `devops-lead` persona as main-thread. Empirical test 2026-07-14 (worklog `802ad9fb`) confirmed Cloud UI mobile has no per-session agent-selector; a repo-scoped `.claude/settings.json` with `agent: devops-lead` is the only reliable path.

## Canonical source

The persona file at `.claude/agents/devops-lead.md` is **synced from vault canonical**:

- Vault repo: [`NovaCaelum-Founder/novacaelum.co-vault`](https://github.com/NovaCaelum-Founder/novacaelum.co-vault)
- Canonical path: `AgentSecretBase/_agentOS/agent_profiles/devops-lead/devops-lead.md`

**Do NOT edit this repo's persona file directly.** Edits go to the vault canonical layer, then sync to this repo via the `deploy-cloud` script (docket task `2ab3b69a`; interim manual sync until that ships).

## Runtime context

DevOps-Lead has two runtime substrates:

1. **`claude-code-action`** (Anthropic-official GitHub Action) — auto-fires on `on: pull_request` against the vault repo (workflow YAML lives in vault at `.github/workflows/devops-lead.yml`).
2. **Cloud UI (this repo)** — Daniel-opened for interactive review, self-test, or task-queue triage (v0.2+ only).

## Version

**v0.1 (LIVE):** PR-review + auto-merge/escalate primary loop + Tertiary self-test.
**v0.2 (DEFERRED):** Secondary Supabase task-queue triage + `on: schedule` cron + self-review discipline loop.

See persona body § "Version status" for full scope map.
