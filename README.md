# devops-lead

A pull-request reviewer that runs Claude Code inside GitHub Actions. It reads the diff, classifies it against a per-repo policy, and then does one of two things: merges it, or labels it for a human and explains why.

It is the review gate for Nova Caelum's own repositories. It is published as a working reference, not as a turnkey product.

## How it works

`.github/workflows/devops-lead-pr-review.yml` is a reusable workflow (`on: workflow_call`). A calling repo keeps a short stub workflow that triggers on its pull requests and calls this one. The review runs in the caller's context, with the caller's secrets and `GITHUB_TOKEN`.

For each pull request, the reviewer:

1. Reads its persona file, which sets its review discipline and tone.
2. Reads the diff with `gh pr diff`.
3. Classifies the change against the caller's policy:
   - **auto-merge-safe** paths, such as docs and README changes
   - **escalate** paths, such as workflows and configuration. It also always escalates a diff that contains credential-shaped strings, and a diff it cannot confidently classify.
4. Posts a verdict comment with a one-line change summary and, if it escalates, a one-line reason.
5. Either queues a squash auto-merge, or applies an escalation label and stops.

Deterministic workflow steps, not the model, then record the outcome and send the escalation notice.

## Guardrails

- **Narrow tools.** The model may only read files and run `gh pr diff`, `view`, `comment`, `merge`, `edit` and `label create`, plus inline review comments. It cannot run arbitrary shell commands.
- **No secrets in the model's hands.** Commands that expand environment variables are denied, so the model cannot make authenticated calls itself. Logging and notification run in separate workflow steps, where secrets stay out of the prompt.
- **Bot pull requests never auto-merge** unless a human has added a `human-approved` label.
- **Tamper-safe persona.** The persona is read from the caller's default branch or a pinned external ref, never from the pull request under review.
- **Pinned dependencies.** The Claude Code Action is pinned to a commit SHA, and the model is pinned by name.

## Calling it

```yaml
# .github/workflows/pr-review.yml in the calling repo
on:
  pull_request:
jobs:
  review:
    uses: Nova-Caelum/devops-lead/.github/workflows/devops-lead-pr-review.yml@<commit-sha>
    secrets: inherit
    with:
      persona_source_repo: Nova-Caelum/devops-lead
      persona_path: .persona-source/.claude/agents/devops-lead.md
      auto_merge_paths: |
        docs/**, README.md
      escalate_paths: |
        .github/workflows/**, src/**
      repo_context_description: "a small web app"
```

Pin to a commit SHA, not `@main`, so a change here is reviewed before it reaches you.

**Required secret:** `CLAUDE_CODE_OAUTH_TOKEN`.

**Adapting it:** the outcome-logging and notification steps are wired to Nova Caelum's internal services. Anyone else running this should replace or remove those two steps.

## Repository layout

| Path | What it is |
|---|---|
| `.github/workflows/devops-lead-pr-review.yml` | The reusable review workflow |
| `.claude/agents/devops-lead.md` | The reviewer's persona. It is maintained in a private source and synced here, so edit it at the source rather than in this repo. |
| `.claude/settings.json` | Opens this repo in Claude Code with the `devops-lead` agent as the main thread |

## Status

v0.1 is live: review, classify, and merge or escalate. Scheduled maintenance runs are on the roadmap.
