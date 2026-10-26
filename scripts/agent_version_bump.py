#!/usr/bin/env python3
"""Agent version bump: +1 (shown +0.01) to agent_registry.agent_version for every agent whose loaded
configuration a push to Nova-Caelum/_agentOS main touched.

Each bump rotates that agent's Caelos Gaetway key (HMAC over `gaetway-agent-key:<agent>:<agent_version>`).
A missed bump is a missed rotation; a double bump on a re-run would refuse live sessions, so the single
write per push carries the idempotency key `agent-version-bump:<after-sha>`.

Both the registry read and the write use the bump-only ops key AGENT_VERSION_BUMP_TOKEN (client
`agent-version-bump-actions`: list_agents, get_agent_info, bump_agent_versions).

The path -> agent mapping is read live from agent_registry (raw rows, include_core=false, plus the
`core_claude` row). Nothing here names an agent:
  agent_profiles/<dir>/<file>.md          -> the agent in the file's frontmatter `name:`
  agent_profiles/<dir>/<sub>/**           -> the agent whose registry canonical_path covers the path
  skills_library/<skill>/**               -> agents whose `skills` list it; core_claude's -> the core group
  hooks_library/**  (by file name)        -> agents whose `hooks` list it;  core_claude's -> the core group
  system/olympus1-scope/native_hermes_plugins/<plugin>/** -> agents whose `hooks` list plugin:<plugin>
  system/vault-scope/vault-mcp.json       -> subscribers (`mcp_servers`) of each server entry that changed
  system/bin/*.sh                         -> subscribers of the vault-mcp.json servers that reference it
  CORE_PREFIXES / CORE_PATHS              -> the core group
The core group is every non-Retired row whose harness equals core_claude's. Retired agents never bump.
IGNORE_* paths never reach an agent's runtime. Anything neither mapped nor ignored is UNMAPPED: it is
printed, raises a ::warning:: and goes to Daniel's Telegram in one message per run — never dropped silently.
Paths are escaped wherever they reach a log line, so a path can never open a workflow command.

Exit codes: 0 done (UNMAPPED paths warn, they do not fail) · 1 a git, registry or write failure, or a
write the verb answered with refused/moved agents · 2 usage or a missing secret. Output is fixed strings plus our own paths and agent names; it never echoes
a server's text. Under --counts-only (any run whose log is public) it prints counts only.
"""
import argparse
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
import json
import os
from pathlib import PurePosixPath
import re
import subprocess
import sys
import urllib.error
import urllib.request

from deferred_checks import TelegramError, send_telegram  # the existing DevOps-Lead Telegram path

OPS = 'https://nova-caelum-ops-production.up.railway.app/mcp'
WRITE_VERB = 'bump_agent_versions'
CORE_ROW = 'core_claude'
RETIRED = 'Retired'
NULL_SHA = '0' * 40
UNMAPPED_ALERT_PATHS = 10
SHA = re.compile(r'^[0-9a-f]{40}$')

MCP_CONFIG = 'system/vault-scope/vault-mcp.json'
CORE_PREFIXES = ('commands_library/', 'core_text/', 'rules_library/', 'system/user-scope/')
CORE_PATHS = {
    'system/deploy-claude_mac', 'system/deploy.json', 'system/skills_gate.py',  # the deployer, its config and its gate
    'system/vault-scope/vault-CLAUDE.md', 'system/launcher-scope/launcher-settings.template.json',
}
IGNORE_PREFIXES = (
    '.github/', 'docs/',
    'graph_library/',                  # Graph Machine and MCP server implementation, not agent configuration
    'hooks_library/ledgers/',          # runtime state; the deployer never overwrites it
    'system/github-actions_library/', 'system/tests_library/',
)
IGNORE_PATHS = {
    '.gitattributes', '.gitignore', '.gitleaksignore',
    'system/git-promote.sh',                             # delivery machinery
    'system/launcher-scope/testbench-settings.json',     # the _testbench launcher is not an agent
}
IGNORE_NAMES = ('README.md', '_README.md', '*.NOTE.md', 'test_*.py', '*_test.py', '.DS_Store', '*.pyc')
IGNORE_DIRS = {'tests', '__pycache__'}


class OpsError(Exception):
    """A failed ops call. `cls` is a fixed class string, safe for a public log."""

    def __init__(self, tool, cls):
        super().__init__(f'{tool}: {cls}')
        self.tool, self.cls = tool, cls


class GitError(Exception):
    pass


# ── registry ─────────────────────────────────────────────────────────────────
def items(value):
    """Names in a registry list column. Tolerates the live quirks: newlines, missing spaces, `[unpopulated]`,
    `Event:file` hooks, `plugin:<name> (...)` entries and trailing prose — the leading name of each item counts."""
    names = set()
    for part in re.split(r'[,;|\n]', value or ''):
        m = re.match(r'\s*([A-Za-z0-9_.:@-]+)', part)
        if m:
            names.add(m.group(1).rsplit(':', 1)[-1])
    names.discard('')
    return names


class Registry:
    def __init__(self, rows, core):
        if not core.get('harness'):
            raise ValueError('core row carries no harness')
        self.rows, self.core = rows, core
        self.live = {n for n, r in rows.items() if r.get('proposed_lifecycle') != RETIRED}
        self.core_group = {n for n in self.live if rows[n].get('harness') == core['harness']}

    def listers(self, column, name):
        """(live agents, whether any row at all lists it) for a skill, hook, plugin or MCP server."""
        listed = {n for n, r in self.rows.items() if name in items(r.get(column))}
        if name in items(self.core.get(column)):
            return (listed & self.live) | self.core_group, True
        return listed & self.live, bool(listed)

    def owner_of(self, path):
        """The agent whose registry canonical_path is this path or a directory containing it."""
        for name, row in sorted(self.rows.items()):
            cp = (row.get('canonical_path') or '').strip()
            cp = cp[len('_agentOS/'):] if cp.startswith('_agentOS/') else cp
            if cp and (path == cp or (cp.endswith('/') and path.startswith(cp))):
                return name
        return None


def fetch_registry(rpc):
    names = [a['agent_name'] for a in rpc('list_agents', {})]
    rows = {n: rpc('get_agent_info', {'agent_name': n, 'include_core': False}) for n in names}
    return Registry(rows, rpc('get_agent_info', {'agent_name': CORE_ROW, 'include_core': False}))


# ── the plan ─────────────────────────────────────────────────────────────────
@dataclass
class Plan:
    bumps: dict = field(default_factory=dict)       # agent -> [paths that caused it]
    ignored: list = field(default_factory=list)
    no_agent: list = field(default_factory=list)    # (path, why) — mapped, but no live agent loads it
    unmapped: list = field(default_factory=list)    # (path, why)

    def bump(self, agents, path):
        for a in agents:
            self.bumps.setdefault(a, []).append(path)

    def causing_paths(self):
        return sorted({p for ps in self.bumps.values() for p in ps})


def ignored(path):
    parts = PurePosixPath(path).parts
    return (path in IGNORE_PATHS or path.startswith(IGNORE_PREFIXES)
            or any(fnmatchcase(parts[-1], pat) for pat in IGNORE_NAMES)
            or bool(IGNORE_DIRS & set(parts[:-1]))
            or (path.startswith('system/bin/') and not path.endswith('.sh')))  # the deployer copies only *.sh


def frontmatter_name(text):
    lines = (text or '').splitlines()
    if not lines or lines[0].strip() != '---':
        return None
    for line in lines[1:]:
        if line.strip() == '---':
            return None
        m = re.match(r'name:\s*["\']?([^"\'\s]+)', line)
        if m:
            return m.group(1)
    return None


def mcp_servers(text):
    try:
        return (json.loads(text) if text else {}).get('mcpServers', {})
    except (ValueError, AttributeError):
        return None


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings(v)


class Mapper:
    def __init__(self, repo, before, after, registry):
        self.repo, self.before, self.after, self.reg = repo, before, after, registry
        self._mcp = None

    def show(self, path):
        text = self.repo.show(self.after, path)
        return text if text is not None else self.repo.show(self.before, path)

    def mcp(self):
        if self._mcp is None:
            self._mcp = (mcp_servers(self.repo.show(self.before, MCP_CONFIG)), mcp_servers(self.repo.show(self.after, MCP_CONFIG)))
        return self._mcp

    def servers_agents(self, servers):
        return set().union(*(self.reg.listers('mcp_servers', s)[0] for s in servers))

    def map(self, path):
        """('bump', agents, what) or ('unmapped', None, why). Empty agents = mapped, but nobody live loads it."""
        parts = path.split('/')
        if parts[0] == 'agent_profiles' and len(parts) == 3:
            if not path.endswith('.md'):
                return 'unmapped', None, 'persona directory holds a non-markdown file'
            name = frontmatter_name(self.show(path))
            if not name:
                return 'unmapped', None, 'persona file has no frontmatter name'
            if name not in self.reg.rows:
                return 'unmapped', None, f'persona name {name} is not an agent_registry row'
            return 'bump', {name} & self.reg.live, f'persona of {name}'
        if parts[0] == 'agent_profiles':
            owner = self.reg.owner_of(path)
            if owner is None:
                return 'unmapped', None, 'no agent_registry canonical_path covers this path'
            return 'bump', {owner} & self.reg.live, f'profile tree of {owner}'
        if parts[0] == 'skills_library' and len(parts) >= 3:
            return 'bump', self.reg.listers('skills', parts[1])[0], f'skill {parts[1]}'
        if parts[0] == 'hooks_library':
            agents, listed = self.reg.listers('hooks', parts[-1])
            if not listed:
                return 'unmapped', None, f'hook file {parts[-1]} is named in no agent_registry hooks column'
            return 'bump', agents, f'hook {parts[-1]}'
        if path.startswith('system/olympus1-scope/native_hermes_plugins/') and len(parts) >= 5:
            agents, listed = self.reg.listers('hooks', parts[3])
            if not listed:
                return 'unmapped', None, f'plugin {parts[3]} is named in no agent_registry hooks column'
            return 'bump', agents, f'plugin {parts[3]}'
        if path == MCP_CONFIG:
            before, after = self.mcp()
            if before is None or after is None:
                return 'unmapped', None, 'vault-mcp.json does not parse at one of the two revisions'
            changed = sorted(s for s in set(before) | set(after) if before.get(s) != after.get(s))
            return 'bump', self.servers_agents(changed), 'mcp servers ' + (', '.join(changed) or '(none changed)')
        if path.startswith('system/bin/') and len(parts) == 3:
            servers = sorted({s for cfg in self.mcp() if cfg for s, entry in cfg.items()
                              if any(v == parts[2] or v.endswith('/' + parts[2]) for v in strings(entry))})
            if not servers:
                return 'unmapped', None, f'{parts[2]} is referenced by no vault-mcp.json server'
            return 'bump', self.servers_agents(servers), 'helper of mcp servers ' + ', '.join(servers)
        if path in CORE_PATHS or path.startswith(CORE_PREFIXES):
            return 'bump', set(self.reg.core_group), 'core'
        return 'unmapped', None, 'no rule maps this path'


def build_plan(repo, before, after, registry):
    plan, mapper = Plan(), Mapper(repo, before, after, registry)
    for path in sorted(set(repo.changed_paths(before, after))):
        if ignored(path):
            plan.ignored.append(path)
            continue
        kind, agents, why = mapper.map(path)
        if kind == 'unmapped':
            plan.unmapped.append((path, why))
        elif agents:
            plan.bump(agents, path)
        else:
            plan.no_agent.append((path, f'{why}: no live agent loads it'))
    return plan


# ── transports (injected in tests) ───────────────────────────────────────────
class GitRepo:
    def __init__(self, root):
        self.root = root

    def _git(self, *args):
        return subprocess.run(['git', '-C', self.root, *args], capture_output=True, timeout=60)

    def changed_paths(self, before, after):
        for rev, label in ((before, 'before'), (after, 'after')):
            if self._git('cat-file', '-e', rev + '^{commit}').returncode:
                raise GitError(f'the {label} commit is not in the checkout')
        res = self._git('diff', '--name-only', '--no-renames', '-z', before, after)
        if res.returncode:
            raise GitError('git diff failed')
        return [p for p in res.stdout.decode('utf-8', 'surrogateescape').split('\0') if p]

    def show(self, rev, path):
        res = self._git('show', f'{rev}:{path}')
        return res.stdout.decode('utf-8', 'replace') if res.returncode == 0 else None


def make_rpc(token, url=OPS):
    """One ops tools/call. Failures raise OpsError with a fixed class; the server's own text never surfaces."""
    def rpc(name, args):
        body = {'jsonrpc': '2.0', 'id': 'agent-version-bump', 'method': 'tools/call',
                'params': {'name': name, 'arguments': args}}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
            'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
            'Authorization': 'Bearer ' + token})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                outer = json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                data = (json.load(e).get('error') or {}).get('data') or {}
            except (ValueError, AttributeError):
                data = {}
            raise OpsError(name, 'tool_not_allowed' if data.get('error') == 'tool_not_allowed' else f'http {e.code}') from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise OpsError(name, 'transport') from None
        if outer.get('error'):
            raise OpsError(name, 'tool_not_found' if outer['error'].get('code') == -32601 else 'rpc error')
        result = outer.get('result') or {}
        if result.get('isError'):
            raise OpsError(name, 'refused')
        try:
            return json.loads(result['content'][0]['text'])
        except (KeyError, IndexError, TypeError, ValueError):
            raise OpsError(name, 'unreadable reply') from None
    return rpc


# ── the run ──────────────────────────────────────────────────────────────────
def cmd_data(text):
    """Escape a workflow-command message (and any line a path reaches): a path can never start a new `::` line."""
    return text.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def cmd_prop(text):
    return cmd_data(text).replace(':', '%3A').replace(',', '%2C')


def report(plan, before, after, n_paths, counts_only, out):
    if counts_only:
        out(f'agent-version-bump: paths={n_paths} agents={len(plan.bumps)} causing_paths={len(plan.causing_paths())} '
            f'ignored={len(plan.ignored)} no_agent={len(plan.no_agent)} unmapped={len(plan.unmapped)}')
        if plan.unmapped:
            out(f'::warning title={cmd_prop("agent-version-bump")}::{len(plan.unmapped)} UNMAPPED path(s); '
                'rerun locally or read the caller run for names')
        return
    out(f'agent-version-bump _agentOS {before[:12]}..{after[:12]}: {n_paths} changed path(s)')
    for agent, paths in sorted(plan.bumps.items()):
        out(f'BUMP {agent} <- ' + cmd_data(', '.join(paths)))
    for path, why in plan.no_agent:
        out(cmd_data(f'NO-AGENT {path} ({why})'))
    for path, why in plan.unmapped:
        out(cmd_data(f'UNMAPPED {path} ({why})'))
        out(f'::warning title={cmd_prop("agent-version-bump UNMAPPED")}::{cmd_data(f"{path}: {why}")}')
    out(f'IGNORED {len(plan.ignored)} path(s)' + (': ' + cmd_data(', '.join(plan.ignored)) if plan.ignored else ''))


def alert_unmapped(plan, after, notify, dry_run, out):
    """Build Gospel rule 1: an UNMAPPED path reaches Daniel, not only a ::warning:: on a green run."""
    if not plan.unmapped:
        return 0
    if notify is None:
        if dry_run:
            out('UNMAPPED alert not sent: dry run without Telegram secrets')
            return 0
        out('UNMAPPED ALERT FAILED: missing Telegram secret(s)')
        return 1
    lines = [f'Agent version bump: UNMAPPED path(s) in _agentOS {after[:12]}']
    lines += [f'- {path}' for path, _ in plan.unmapped[:UNMAPPED_ALERT_PATHS]]
    lines.append(f'{len(plan.unmapped)} UNMAPPED path(s) in total. No rule maps them, so they bumped nobody; '
                 'the run summary has the reasons.')
    try:
        notify('\n'.join(lines))
    except TelegramError as e:
        out(f'UNMAPPED ALERT FAILED: telegram {e}')
        return 1
    out('UNMAPPED alert sent')
    return 0


def write_summary(plan, args_payload, outcome):
    target = os.environ.get('GITHUB_STEP_SUMMARY')
    if not target:
        return
    lines = ['### Agent version bump', '', f'**{outcome}**', '', '| agent | paths |', '|---|---|']
    lines += [f'| {a} | {"<br>".join(ps)} |' for a, ps in sorted(plan.bumps.items())] or ['| (none) | |']
    if plan.unmapped:
        lines += ['', '**UNMAPPED** (no rule maps these; registry or mapping gap):', '']
        lines += [f'- `{p}`: {why}' for p, why in plan.unmapped]
    if plan.no_agent:
        lines += ['', 'Mapped, no live agent loads them:', ''] + [f'- `{p}`: {why}' for p, why in plan.no_agent]
    if args_payload:
        lines += ['', f'`idempotency_key={args_payload["idempotency_key"]}`']
    with open(target, 'a', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')


def run(repo, before, after, rpc, *, dry_run=False, counts_only=False, notify=None, out=print):
    if before == NULL_SHA:
        out('ERROR: before is the null SHA (a branch creation or a ref with no prior commit); refusing to guess the range')
        return 1
    if not (SHA.match(before or '') and SHA.match(after or '')):
        out('ERROR: before and after must be full 40-character commit SHAs')
        return 2
    try:
        paths = repo.changed_paths(before, after)
    except GitError as e:
        out(f'ERROR: git: {e}')
        return 1
    if not paths:
        out('agent-version-bump: no changed paths; nothing to bump')
        return 0
    try:
        registry = fetch_registry(rpc)
    except OpsError as e:
        out(f'REGISTRY READ FAILED: {e.tool} ({e.cls}). This key needs list_agents and get_agent_info. Nothing was bumped.')
        return 1
    except (KeyError, TypeError, ValueError):
        out('REGISTRY READ FAILED: the registry reply has an unexpected shape. Nothing was bumped.')
        return 1
    plan = build_plan(repo, before, after, registry)
    report(plan, before, after, len(paths), counts_only, out)
    alerted = alert_unmapped(plan, after, notify, dry_run, out)
    return max(alerted, write(plan, after, rpc, dry_run, counts_only, out))


def write(plan, after, rpc, dry_run, counts_only, out):
    if not plan.bumps:
        out('agent-version-bump: nothing to bump')
        write_summary(plan, None, 'nothing to bump')
        return 0
    payload = {'agent_names': sorted(plan.bumps), 'reason': f'_agentOS {after}: {len(plan.causing_paths())} path(s)',
               'idempotency_key': f'agent-version-bump:{after}'}
    if dry_run:
        out(f'DRY-RUN: would call {WRITE_VERB} for {len(payload["agent_names"])} agent(s), '
            f'idempotency_key={payload["idempotency_key"]}; nothing written')
        write_summary(plan, payload, 'dry run: nothing written')
        return 0
    try:
        result = rpc(WRITE_VERB, payload)
    except OpsError as e:
        if e.cls in ('tool_not_allowed', 'tool_not_found'):
            out(f'WRITE FAILED: {WRITE_VERB} is not available to this key ({e.cls}). The ops verb has not landed, '
                'or this key is not granted it. Nothing was bumped.')
        else:
            out(f'WRITE FAILED: {WRITE_VERB} ({e.cls}). Outcome unknown; re-running this push is safe '
                f'(idempotency_key={payload["idempotency_key"]}).')
        write_summary(plan, payload, f'WRITE FAILED ({e.cls})')
        return 1
    if not (isinstance(result, dict) and isinstance(result.get('bumped'), dict)):
        out(f'WRITE FAILED: the {WRITE_VERB} reply has an unexpected shape. Re-running this push is safe '
            f'(idempotency_key={payload["idempotency_key"]}).')
        write_summary(plan, payload, 'WRITE FAILED (unexpected reply)')
        return 1
    bumped, refused, moved = result['bumped'], result.get('refused') or {}, result.get('moved') or {}
    replay = ' (replay: first result returned, nothing bumped twice)' if result.get('replayed') else ''
    if counts_only:
        out(f'WROTE {WRITE_VERB}: bumped={len(bumped)} refused={len(refused)} moved={len(moved)}{replay}')
    else:
        out(f'WROTE {WRITE_VERB}{replay}: ' + (', '.join(f'{a}={v / 100:.2f}' for a, v in sorted(bumped.items())) or 'none'))
        for agent, why in sorted(refused.items()):
            out(f'REFUSED {agent} ({why})')
        for agent, state in sorted(moved.items()):
            out(f'MOVED {agent} (planned {state.get("planned")}, now {state.get("current")})')
    if refused or moved:
        out(f'WRITE INCOMPLETE: {len(refused)} refused, {len(moved)} moved since the registry read. '
            'The registry changed under this run; nothing will be bumped twice, but these agents need a look.')
        write_summary(plan, payload, f'WRITE INCOMPLETE ({len(refused)} refused, {len(moved)} moved)')
        return 1
    write_summary(plan, payload, f'written: {len(bumped)} agent(s) bumped{replay}')
    return 0


def telegram():
    token, chat = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_DANIEL_CHAT_ID')
    return (lambda text: send_telegram(token, chat, text)) if token and chat else None


def notify_failure(after, run_url):
    notify = telegram()
    if notify is None:
        print('ERROR: missing Telegram secret(s)')
        return 2
    text = (f'Agent version bump FAILED for _agentOS {(after or "?")[:12]}. Agents whose config changed may not '
            f'have rotated their gateway key. Run: {run_url or "(unknown)"}')
    try:
        notify(text)
    except TelegramError as e:
        print(f'ERROR: telegram {e}')
        return 1
    print('alert sent')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--repo', default='.', help='checkout of Nova-Caelum/_agentOS holding both commits')
    ap.add_argument('--before', help='the push\'s previous main head')
    ap.add_argument('--after', help='the push\'s new main head')
    ap.add_argument('--dry-run', action='store_true', help='print the plan; write nothing')
    ap.add_argument('--counts-only', action='store_true', help='print counts only (public logs)')
    ap.add_argument('--notify-failure', action='store_true', help='send the failure alert to Telegram and exit')
    ap.add_argument('--run-url', help='link included in the failure alert')
    args = ap.parse_args(argv)
    if args.notify_failure:
        return notify_failure(args.after, args.run_url)
    if not (args.before and args.after):
        ap.error('--before and --after are required')
    token = os.environ.get('AGENT_VERSION_BUMP_TOKEN')  # the bump-only ops key; the PR-review key is refused by the verb
    if not token:
        print('ERROR: missing AGENT_VERSION_BUMP_TOKEN')
        return 2
    return run(GitRepo(args.repo), args.before, args.after, make_rpc(token),
               dry_run=args.dry_run, counts_only=args.counts_only, notify=telegram())


if __name__ == '__main__':
    sys.exit(main())
