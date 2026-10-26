import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))
import agent_version_bump as avb  # noqa: E402

BEFORE = 'a' * 40
AFTER = 'b' * 40

# A fixture registry shaped like the live rows, quirks included: designer's skills carry a newline and a
# missing space, `[unpopulated]` stands in for empty, hooks are `Event:file`, hermes hooks are `plugin:<name> (...)`.
CORE = {'agent_name': 'core_claude', 'harness': 'claude-code', 'proposed_lifecycle': None,
        'skills': 'assumption-check, web-search', 'mcp_servers': 'nova-caelum-ops, exa',
        'hooks': 'SessionStart:session-start.sh, PreToolUse:cardinal-guard.sh'}
ROWS = {
    'chief-pm': {'harness': 'claude-code', 'proposed_lifecycle': 'Beta', 'skills': 'adhd-support, option-conception',
                 'hooks': 'UserPromptSubmit:numeric-ref-guard.sh, Stop:numeric-ref-guard.sh', 'mcp_servers': ''},
    'designer': {'harness': 'claude-code', 'proposed_lifecycle': 'Beta', 'skills': 'refactoring-ui, \nimpeccable,ux-heuristics',
                 'hooks': '[unpopulated]', 'mcp_servers': 'playwright, fal-ai'},
    'engineer': {'harness': 'claude-code', 'proposed_lifecycle': 'Beta', 'skills': 'adhd-support, engineering-code-review',
                 'hooks': 'UserPromptSubmit:numeric-ref-guard.sh', 'mcp_servers': None},
    'librarian': {'harness': 'claude-code', 'proposed_lifecycle': 'Retired', 'skills': 'engineering-code-review, legacy-skill',
                  'hooks': '[unpopulated]', 'mcp_servers': 'nova-cloud-vault'},
    'vulcan': {'harness': 'hermes-agent', 'proposed_lifecycle': 'Beta', 'skills': 'find-docs, assumption-check',
               'hooks': 'plugin:agent-registry-guard (pre_tool_call, post_tool_call); plugin:nc-vault-flood-guard (pre_tool_call)',
               'mcp_servers': 'context7', 'canonical_path': '_agentOS/agent_profiles/hermes_profiles/vulcan/'},
    'codex': {'harness': 'codex', 'proposed_lifecycle': 'Test', 'skills': 're0',
              'hooks': 'PreToolUse:cloudflare-deploy-pointer.py (/etc/codex managed)', 'mcp_servers': None},
}
CORE_GROUP = {'chief-pm', 'designer', 'engineer'}

MCP_BEFORE = {'mcpServers': {
    'nova-caelum-ops': {'type': 'http', 'url': 'https://ops.example/mcp', 'headersHelper': '{{HOME}}/.claude/bin/ops-headers.sh'},
    'exa': {'command': '{{HOME}}/.claude/bin/mcp-run.sh', 'args': ['id', 'EXA_API_KEY', '--', 'npx', '-y', 'exa-mcp-server@3.2.1']},
    'fal-ai': {'type': 'http', 'url': 'https://fal.example/mcp', 'headersHelper': '{{HOME}}/.claude/bin/fal-headers.sh'},
    'playwright': {'command': 'npx', 'args': ['-y', '@playwright/mcp@0.0.77']},
}}


def persona(name):
    return f'---\nname: {name}\ndescription: test persona\n---\n\n# Body\n'


def registry():
    return avb.Registry(json.loads(json.dumps(ROWS)), dict(CORE))


class FakeRepo:
    """The two git reads the script makes: the changed-path list and a file at a revision."""

    def __init__(self, changes, before=None, after=None):
        self.changes = list(changes)
        self.files = {BEFORE: dict(before or {}), AFTER: dict(after or {})}

    def changed_paths(self, before, after):
        return list(self.changes)

    def show(self, rev, path):
        return self.files[rev].get(path)


class RecordingRpc:
    def __init__(self, write_error=None, read_error=None, write_reply=None):
        self.calls, self.write_error, self.read_error, self.write_reply = [], write_error, read_error, write_reply

    def __call__(self, name, args):
        self.calls.append((name, args))
        if name == 'list_agents':
            if self.read_error:
                raise avb.OpsError(name, self.read_error)
            return [{'agent_name': n} for n in ROWS]
        if name == 'get_agent_info':
            return CORE if args['agent_name'] == 'core_claude' else {'agent_name': args['agent_name'], **ROWS[args['agent_name']]}
        if name == 'bump_agent_versions':
            if self.write_error:
                raise avb.OpsError(name, self.write_error)
            if self.write_reply is not None:
                return self.write_reply
            return {'bumped': {n: 101 for n in args['agent_names']}, 'refused': {}, 'moved': {},
                    'idempotency_key': args['idempotency_key'], 'replayed': False, 'worklog_id': 'w1'}
        raise AssertionError(name)

    def writes(self):
        return [a for n, a in self.calls if n == 'bump_agent_versions']


def plan_for(changes, before=None, after=None):
    return avb.build_plan(FakeRepo(changes, before, after), BEFORE, AFTER, registry())


def run(changes, rpc=None, before=None, after=None, **kw):
    rpc = rpc or RecordingRpc()
    buf = io.StringIO()
    kw.setdefault('notify', Notifier())
    with redirect_stdout(buf):
        code = avb.run(FakeRepo(changes, before, after), BEFORE, AFTER, rpc, **kw)
    return code, buf.getvalue(), rpc


class MappingTests(unittest.TestCase):
    def test_persona_file_bumps_exactly_the_agent_named_in_its_frontmatter(self):
        path = 'agent_profiles/chief-pm/chief-pm.md'
        plan = plan_for([path], after={path: persona('chief-pm')})
        self.assertEqual(plan.bumps, {'chief-pm': [path]})
        self.assertEqual(plan.unmapped, [])

    def test_persona_directory_name_is_not_trusted_frontmatter_name_is(self):
        path = 'agent_profiles/McGuffin/mcguffin.md'
        plan = plan_for([path], after={path: persona('engineer')})
        self.assertEqual(set(plan.bumps), {'engineer'})

    def test_deleted_persona_is_named_from_the_before_revision(self):
        path = 'agent_profiles/designer/designer.md'
        plan = plan_for([path], before={path: persona('designer')})
        self.assertEqual(set(plan.bumps), {'designer'})

    def test_persona_name_that_is_not_a_registry_row_is_unmapped(self):
        path = 'agent_profiles/hermes_profiles/Legacy_chief-of-staff.md'
        plan = plan_for([path], after={path: persona('chief-of-staff')})
        self.assertEqual(plan.bumps, {})
        self.assertEqual([p for p, _ in plan.unmapped], [path])

    def test_core_skill_bumps_the_whole_core_group_plus_other_listers(self):
        plan = plan_for(['skills_library/assumption-check/SKILL.md'])
        self.assertEqual(set(plan.bumps), CORE_GROUP | {'vulcan'})

    def test_non_core_skill_bumps_only_the_agents_that_list_it(self):
        plan = plan_for(['skills_library/adhd-support/SKILL.md', 'skills_library/impeccable/references/x.md'])
        self.assertEqual(set(plan.bumps), {'chief-pm', 'engineer', 'designer'})
        self.assertEqual(plan.bumps['designer'], ['skills_library/impeccable/references/x.md'])

    def test_skill_no_live_agent_loads_is_reported_not_bumped_and_not_unmapped(self):
        plan = plan_for(['skills_library/nobody-loads-me/SKILL.md'])
        self.assertEqual(plan.bumps, {})
        self.assertEqual([p for p, _ in plan.no_agent], ['skills_library/nobody-loads-me/SKILL.md'])
        self.assertEqual(plan.unmapped, [])

    def test_hook_bumps_the_agents_whose_hooks_column_names_it(self):
        plan = plan_for(['hooks_library/numeric-ref-guard.sh'])
        self.assertEqual(set(plan.bumps), {'chief-pm', 'engineer'})

    def test_core_hook_bumps_the_whole_core_group(self):
        plan = plan_for(['hooks_library/cardinal-guard.sh'])
        self.assertEqual(set(plan.bumps), CORE_GROUP)

    def test_hook_with_a_parenthesised_note_still_matches(self):
        plan = plan_for(['hooks_library/cloudflare-deploy-pointer.py'])
        self.assertEqual(set(plan.bumps), {'codex'})

    def test_hook_no_registry_row_lists_is_unmapped(self):
        plan = plan_for(['hooks_library/taskgraph-audit.sh', 'hooks_library/lib/load-secrets.sh'])
        self.assertEqual(plan.bumps, {})
        self.assertEqual(len(plan.unmapped), 2)

    def test_hermes_plugin_maps_through_the_hooks_column(self):
        plan = plan_for(['system/olympus1-scope/native_hermes_plugins/nc-vault-flood-guard/__init__.py'])
        self.assertEqual(set(plan.bumps), {'vulcan'})

    def test_hermes_profile_tree_maps_through_registry_canonical_path(self):
        plan = plan_for(['agent_profiles/hermes_profiles/vulcan/skills/research/signal-intake/SKILL.md',
                         'agent_profiles/hermes_profiles/_shared/drive-map.md'])
        self.assertEqual(set(plan.bumps), {'vulcan'})
        self.assertEqual([p for p, _ in plan.unmapped], ['agent_profiles/hermes_profiles/_shared/drive-map.md'])

    def test_core_rule_bumps_the_whole_core_group(self):
        for path in ('rules_library/project/rules/concision.md', 'core_text/daniel.md', 'commands_library/handle.md',
                     'system/user-scope/user-settings.json', 'system/vault-scope/vault-CLAUDE.md',
                     'system/launcher-scope/launcher-settings.template.json', 'system/deploy-claude_mac'):
            with self.subTest(path=path):
                self.assertEqual(set(plan_for([path]).bumps), CORE_GROUP)

    def test_retired_agent_is_never_bumped(self):
        plan = plan_for(['skills_library/engineering-code-review/SKILL.md', 'skills_library/legacy-skill/SKILL.md',
                         'agent_profiles/librarian/librarian.md', 'rules_library/project/rules/concision.md'],
                        after={'agent_profiles/librarian/librarian.md': persona('librarian')})
        self.assertNotIn('librarian', plan.bumps)
        self.assertEqual(set(plan.bumps), CORE_GROUP)
        self.assertIn('skills_library/legacy-skill/SKILL.md', [p for p, _ in plan.no_agent])

    def test_ignored_paths_map_to_nothing(self):
        paths = ['.github/workflows/x.yml', 'README.md', 'skills_library/re0/README.md', 'docs/a.md',
                 'graph_library/judgment/x.py', 'system/tests_library/t.py', 'system/bin/loop_state.py',
                 'skills_library/re0/tests/test_x.py', 'system/olympus1-scope/native_hermes_plugins/p/test_p.py',
                 'system/vault-scope/vault-mcp.novacomms.NOTE.md', 'system/git-promote.sh']
        plan = plan_for(paths)
        self.assertEqual(plan.bumps, {})
        self.assertEqual(sorted(plan.ignored), sorted(paths))
        self.assertEqual(plan.unmapped, [])

    def test_unknown_path_is_unmapped(self):
        plan = plan_for(['brand_new_dir/thing.md', 'skills_library/loose-file.md'])
        self.assertEqual(plan.bumps, {})
        self.assertEqual([p for p, _ in plan.unmapped], ['brand_new_dir/thing.md', 'skills_library/loose-file.md'])

    def test_mcp_config_bumps_only_subscribers_of_the_server_entries_that_changed(self):
        after = json.loads(json.dumps(MCP_BEFORE))
        after['mcpServers']['fal-ai']['url'] = 'https://fal.example/v2/mcp'
        path = 'system/vault-scope/vault-mcp.json'
        plan = plan_for([path], before={path: json.dumps(MCP_BEFORE)}, after={path: json.dumps(after)})
        self.assertEqual(set(plan.bumps), {'designer'})

    def test_mcp_helper_maps_through_the_server_that_references_it(self):
        path = 'system/vault-scope/vault-mcp.json'
        files = {path: json.dumps(MCP_BEFORE)}
        self.assertEqual(set(plan_for(['system/bin/ops-headers.sh'], files, files).bumps), CORE_GROUP)
        self.assertEqual(set(plan_for(['system/bin/fal-headers.sh'], files, files).bumps), {'designer'})
        unref = plan_for(['system/bin/handle.sh'], files, files)
        self.assertEqual((unref.bumps, [p for p, _ in unref.unmapped]), ({}, ['system/bin/handle.sh']))


class ParseTests(unittest.TestCase):
    def test_registry_columns_parse_through_their_quirks(self):
        self.assertEqual(avb.items('refactoring-ui, \nimpeccable,ux-heuristics'), {'refactoring-ui', 'impeccable', 'ux-heuristics'})
        self.assertEqual(avb.items('[unpopulated]'), set())
        self.assertEqual(avb.items(None), set())
        self.assertEqual(avb.items('UserPromptSubmit:concision-budget.sh, Stop:concision-stop.sh'),
                         {'concision-budget.sh', 'concision-stop.sh'})
        self.assertIn('agent-registry-guard', avb.items('plugin:agent-registry-guard (pre_tool_call, post_tool_call); plugin:x'))
        self.assertIn('context7', avb.items('context7 (IN - required by find-docs-nous), sequential_thinking'))


class RunTests(unittest.TestCase):
    def test_whole_path_writes_once_with_the_push_keyed_idempotency(self):
        path = 'agent_profiles/chief-pm/chief-pm.md'
        code, out, rpc = run([path, 'README.md'], after={path: persona('chief-pm')})
        self.assertEqual(code, 0)
        self.assertEqual(rpc.writes(), [{'agent_names': ['chief-pm'], 'reason': f'_agentOS {AFTER}: 1 path(s)',
                                         'idempotency_key': f'agent-version-bump:{AFTER}'}])
        self.assertIn('BUMP chief-pm', out)
        get_info = [a for n, a in rpc.calls if n == 'get_agent_info']
        self.assertTrue(all(a['include_core'] is False for a in get_info))
        self.assertIn({'agent_name': 'core_claude', 'include_core': False}, get_info)

    def test_rerun_of_the_same_push_sends_the_same_key(self):
        path = 'core_text/daniel.md'
        first, second = run([path])[2].writes(), run([path])[2].writes()
        self.assertEqual(first, second)
        self.assertEqual(first[0]['agent_names'], sorted(CORE_GROUP))

    def test_empty_diff_makes_no_write_call(self):
        code, out, rpc = run([])
        self.assertEqual((code, rpc.writes()), (0, []))
        self.assertIn('nothing to bump', out)

    def test_ignored_only_diff_makes_no_write_call(self):
        code, _, rpc = run(['.github/workflows/x.yml'])
        self.assertEqual((code, rpc.writes()), (0, []))

    def test_dry_run_prints_the_plan_and_writes_nothing(self):
        code, out, rpc = run(['core_text/daniel.md'], dry_run=True)
        self.assertEqual((code, rpc.writes()), (0, []))
        self.assertIn('DRY-RUN', out)
        self.assertIn(f'agent-version-bump:{AFTER}', out)

    def test_missing_ops_verb_fails_loudly_and_echoes_no_server_text(self):
        for cls in ('tool_not_allowed', 'tool_not_found'):
            with self.subTest(cls=cls):
                code, out, rpc = run(['core_text/daniel.md'], rpc=RecordingRpc(write_error=cls))
                self.assertEqual(code, 1)
                self.assertIn('WRITE FAILED', out)
                self.assertIn('bump_agent_versions', out)
                self.assertIn('Nothing was bumped', out)

    def test_written_versions_are_printed(self):
        code, out, _ = run(['core_text/daniel.md'])
        self.assertEqual(code, 0)
        self.assertIn('WROTE bump_agent_versions', out)
        self.assertIn('chief-pm=1.01', out)

    def test_a_reply_with_refused_or_moved_agents_fails_loudly(self):
        for reply in ({'bumped': {'chief-pm': 101}, 'refused': {'designer': 'Retired agent'}, 'moved': {}},
                      {'bumped': {}, 'refused': {}, 'moved': {'chief-pm': {'planned': 101, 'current': 150}}}):
            with self.subTest(reply=reply):
                code, out, _ = run(['core_text/daniel.md'], rpc=RecordingRpc(write_reply=reply))
                self.assertEqual(code, 1)
                self.assertIn('WRITE INCOMPLETE', out)

    def test_a_reply_without_the_bumped_map_fails_loudly(self):
        code, out, _ = run(['core_text/daniel.md'], rpc=RecordingRpc(write_reply={'unexpected': True}))
        self.assertEqual(code, 1)
        self.assertIn('unexpected shape', out)

    def test_counts_only_write_names_no_agent(self):
        code, out, _ = run(['core_text/daniel.md'], counts_only=True)
        self.assertEqual(code, 0)
        self.assertNotIn('chief-pm', out)
        self.assertIn('bumped=3', out)

    def test_registry_read_refusal_fails_before_any_write(self):
        code, out, rpc = run(['core_text/daniel.md'], rpc=RecordingRpc(read_error='tool_not_allowed'))
        self.assertEqual((code, rpc.writes()), (1, []))
        self.assertIn('REGISTRY READ FAILED', out)

    def test_unmapped_path_warns_but_does_not_fail(self):
        code, out, rpc = run(['brand_new_dir/thing.md'])
        self.assertEqual((code, rpc.writes()), (0, []))
        self.assertIn('UNMAPPED brand_new_dir/thing.md', out)
        self.assertIn('::warning', out)

    def test_counts_only_output_names_no_agent_and_no_path(self):
        code, out, _ = run(['agent_profiles/chief-pm/chief-pm.md', 'brand_new_dir/thing.md'],
                           after={'agent_profiles/chief-pm/chief-pm.md': persona('chief-pm')}, dry_run=True, counts_only=True)
        self.assertEqual(code, 0)
        for secretish in ('chief-pm', 'brand_new_dir', 'agent_profiles'):
            self.assertNotIn(secretish, out)
        self.assertIn('agents=1', out)
        self.assertIn('unmapped=1', out)

    def test_null_before_sha_is_refused(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = avb.run(FakeRepo(['core_text/daniel.md']), '0' * 40, AFTER, RecordingRpc())
        self.assertEqual(code, 1)
        self.assertIn('null SHA', buf.getvalue())


class WorkflowCommandTests(unittest.TestCase):
    def test_escaping_follows_the_workflow_command_rules(self):
        self.assertEqual(avb.cmd_data('a%b\r\nc'), 'a%25b%0D%0Ac')
        self.assertEqual(avb.cmd_prop('a:b,c%\n'), 'a%3Ab%2Cc%25%0A')

    def test_a_newline_bearing_path_cannot_open_a_workflow_command(self):
        evil = 'brand_new_dir/x\n::error::pwned\r\n::add-mask::y'
        code, out, _ = run([evil])
        self.assertEqual(code, 0)
        for line in out.splitlines():
            self.assertFalse(line.startswith('::error') or line.startswith('::add-mask'), line)
        warning = [l for l in out.splitlines() if l.startswith('::warning')]
        self.assertEqual(len(warning), 1)
        self.assertIn('%0A::error::pwned', warning[0])


class Notifier:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, text):
        if self.fail:
            raise avb.TelegramError('transport')
        self.sent.append(text)


class UnmappedAlertTests(unittest.TestCase):
    def test_unmapped_paths_send_one_telegram_with_commit_ten_paths_and_count(self):
        paths = [f'brand_new_dir/f{i:02d}.md' for i in range(12)]
        notify = Notifier()
        code, out, _ = run(paths, notify=notify)
        self.assertEqual(code, 0)
        self.assertEqual(len(notify.sent), 1)
        msg = notify.sent[0]
        self.assertIn(AFTER[:12], msg.splitlines()[0])
        listed = [l for l in msg.splitlines() if l.startswith('- ')]
        self.assertEqual(listed, [f'- {p}' for p in paths[:10]])
        self.assertIn('12 UNMAPPED', msg.splitlines()[-1])
        self.assertIn('::warning', out)

    def test_no_unmapped_path_sends_nothing(self):
        notify = Notifier()
        run(['core_text/daniel.md', 'README.md'], notify=notify)
        self.assertEqual(notify.sent, [])

    def test_alert_is_independent_of_the_write(self):
        notify = Notifier()
        code, _, rpc = run(['core_text/daniel.md', 'brand_new_dir/x.md'], notify=notify)
        self.assertEqual((code, len(rpc.writes()), len(notify.sent)), (0, 1, 1))

    def test_a_failed_alert_fails_the_run_but_the_write_still_lands(self):
        code, out, rpc = run(['core_text/daniel.md', 'brand_new_dir/x.md'], notify=Notifier(fail=True))
        self.assertEqual((code, len(rpc.writes())), (1, 1))
        self.assertIn('UNMAPPED ALERT FAILED', out)

    def test_a_live_run_without_telegram_secrets_fails_when_paths_are_unmapped(self):
        code, out, _ = run(['brand_new_dir/x.md'], notify=None)
        self.assertEqual(code, 1)
        self.assertIn('UNMAPPED ALERT FAILED', out)

    def test_a_dry_run_without_telegram_secrets_says_so_and_does_not_fail(self):
        code, out, _ = run(['brand_new_dir/x.md'], notify=None, dry_run=True)
        self.assertEqual(code, 0)
        self.assertIn('UNMAPPED alert not sent', out)


if __name__ == '__main__':
    unittest.main()
