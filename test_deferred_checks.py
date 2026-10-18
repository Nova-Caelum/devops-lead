import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))
import deferred_checks as dc  # noqa: E402

NOW = datetime(2026, 10, 19, 13, 0, tzinfo=timezone.utc)


def entry(**over):
    e = {'row': 'GOV-377', 'project': 'governance-evolution', 'external_id': 'governance-evolution:daily-stale-sweep',
         'evaluator': 'cloud',
         'check': {'type': 'github_workflow_run', 'repo': 'Nova-Caelum/devops-lead', 'workflow': 'daily-stale-sweep.yml',
                   'event': 'schedule', 'conclusion': 'success', 'job': 'sweep', 'created_after': '2026-10-18T00:00:00Z'},
         'evidence_path': 'AgentSecretBase/_ops/deferred-checks/evidence/GOV-377.md',
         'evidence_line': 'workflow=Daily stale sweep event=schedule', 'due': '2026-10-19T12:00:00Z', 'grace_hours': 24}
    e.update(over)
    return e


class FakeGh:
    def __init__(self, runs=(), jobs=None, search=None):
        self.runs, self.jobs, self.search = list(runs), jobs or {}, search or {}

    def __call__(self, args):
        path = args[1]
        if '/runs?' in path:
            event = path.split('event=')[1].split('&')[0]
            return json.dumps({'workflow_runs': [r for r in self.runs if r['event'] == event]})
        if path.endswith('/jobs'):
            run_id = int(path.split('/runs/')[1].split('/')[0])
            return json.dumps({'jobs': self.jobs.get(run_id, [])})
        if path.startswith('search/issues'):
            return json.dumps({'total_count': self.search.get(path, 0)})
        raise AssertionError(path)


def run_obj(rid, event='schedule', conclusion='success', created='2026-10-19T11:17:00Z'):
    return {'id': rid, 'event': event, 'status': 'completed', 'conclusion': conclusion, 'created_at': created,
            'html_url': f'https://github.com/x/runs/{rid}'}


class FakeOps:
    def __init__(self, worklog=(), state='ready'):
        self.worklog, self.state, self.appended = list(worklog), state, []

    def read(self, name, args):
        assert name == 'list_work_items'
        return [{'external_id': 'governance-evolution:daily-stale-sweep', 'state': self.state}]

    def ops(self, name, args):
        if name == 'search_worklog':
            want = set(args['tags'])
            return [w for w in self.worklog if want & set(w.get('tags', []))]
        if name == 'append_worklog':
            self.appended.append(args)
            row = {'id': '00000000-0000-0000-0000-%012d' % len(self.appended), 'tags': args['tags'],
                   'summary': args['summary'], 'created_at': NOW.isoformat()}
            self.worklog.append(row)
            return row
        raise AssertionError(name)


def heartbeat():
    return {'tags': ['deferred-check-heartbeat'], 'summary': 'hb', 'created_at': '2026-10-19T12:00:00+00:00'}


class Tests(unittest.TestCase):
    def go(self, reg, gh, ops, now=NOW, **kw):
        sent = []
        counts = dc.run(reg, gh=gh, read_rpc=ops.read, ops_rpc=ops.ops, send=sent.append, now=now, out=lambda s: None, **kw)
        return counts, sent

    def test_pass_posts_one_verdict_and_no_alarm(self):
        gh = FakeGh([run_obj(5)], {5: [{'name': 'sweep', 'conclusion': 'success'}]})
        ops = FakeOps([heartbeat()])
        counts, sent = self.go([entry()], gh, ops)
        self.assertEqual(counts['pass'], 1)
        self.assertEqual(sent, [])
        self.assertEqual(ops.appended[0]['tags'], ['deferred-check', 'pass', 'row-gov-377'])
        self.assertIn('"run_id": 5', ops.appended[0]['detailed'])

    def test_manual_dispatch_does_not_count_as_schedule(self):
        gh = FakeGh([run_obj(6, event='workflow_dispatch')], {6: [{'name': 'sweep', 'conclusion': 'success'}]})
        ops = FakeOps([heartbeat()])
        counts, sent = self.go([entry()], gh, ops)
        self.assertEqual(counts['fail'], 1)
        self.assertEqual(len(sent), 1)
        self.assertIn('GOV-377 FAIL', sent[0])

    def test_skipped_job_fails(self):
        gh = FakeGh([run_obj(7)], {7: [{'name': 'sweep', 'conclusion': 'skipped'}]})
        counts, _ = self.go([entry()], gh, FakeOps([heartbeat()]))
        self.assertEqual(counts['fail'], 1)

    def test_before_due_a_miss_is_pending_not_fail(self):
        early = datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc)
        counts, sent = self.go([entry()], FakeGh([]), FakeOps([heartbeat()]), now=early)
        self.assertEqual((counts['pending'], counts['fail'], sent), (1, 0, []))

    def test_force_due_judges_now(self):
        early = datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc)
        counts, sent = self.go([entry()], FakeGh([]), FakeOps([heartbeat()]), now=early, force_due=True)
        self.assertEqual(counts['fail'], 1)
        self.assertEqual(len(sent), 1)

    def test_run_before_floor_ignored(self):
        gh = FakeGh([run_obj(8, created='2026-10-17T11:17:00Z')], {8: [{'name': 'sweep', 'conclusion': 'success'}]})
        counts, _ = self.go([entry()], gh, FakeOps([heartbeat()]))
        self.assertEqual(counts['fail'], 1)

    def test_done_row_is_skipped(self):
        counts, sent = self.go([entry()], FakeGh([]), FakeOps([heartbeat()], state='done'))
        self.assertEqual((counts['skipped_done'], sent), (1, []))

    def test_stale_heartbeat_alarms_once_per_day(self):
        ops = FakeOps([])
        _, sent1 = self.go([], FakeGh([]), ops)
        _, sent2 = self.go([], FakeGh([]), ops)
        self.assertEqual(len(sent1), 1)
        self.assertIn('no heartbeat', sent1[0])
        self.assertEqual(sent2, [])

    def test_unapplied_pass_alarms(self):
        old_pass = {'tags': ['deferred-check', 'pass', 'row-gov-377'], 'summary': 'p', 'created_at': '2026-10-18T10:00:00+00:00'}
        counts, sent = self.go([entry()], FakeGh([]), FakeOps([heartbeat(), old_pass]))
        self.assertTrue(any('not applied' in s for s in sent))
        self.assertEqual(counts['pass'] + counts['fail'], 0)  # an existing PASS is not re-evaluated

    def test_overdue_alarms(self):
        late = datetime(2026, 10, 21, 13, 0, tzinfo=timezone.utc)
        _, sent = self.go([entry(evaluator='mac', check={'type': 'local_report'})], FakeGh([]), FakeOps([heartbeat()]), now=late)
        self.assertTrue(any('was due' in s for s in sent))

    def test_mac_rows_are_not_evaluated_in_cloud(self):
        counts, _ = self.go([entry(evaluator='mac', check={'type': 'local_report'})], FakeGh([]), FakeOps([heartbeat()]))
        self.assertEqual(counts['pass'] + counts['fail'] + counts['pending'], 0)

    def test_escalation_rate_drop(self):
        chk = {'type': 'escalation_rate_drop', 'repos': ['o/r'], 'label': 'escalate-daniel',
               'baseline': ['2026-10-07', '2026-10-13'], 'after': ['2026-10-15', '2026-10-21']}
        def q(s, e, lab=False):
            base = f'search/issues?q=repo:o/r+is:pr+created:{s}..{e}'
            return base + ('+label:escalate-daniel' if lab else '') + '&per_page=1'
        gh = FakeGh(search={q('2026-10-07', '2026-10-13'): 10, q('2026-10-07', '2026-10-13', True): 8,
                            q('2026-10-15', '2026-10-21'): 10, q('2026-10-15', '2026-10-21', True): 3})
        self.assertEqual(dc.eval_escalation_rate_drop(gh, chk)[0], 'pass')
        gh.search[q('2026-10-15', '2026-10-21', True)] = 9
        self.assertEqual(dc.eval_escalation_rate_drop(gh, chk)[0], 'fail')

    def test_registry_validation(self):
        with self.assertRaises(ValueError):
            dc.validate(entry(evidence_path='/etc/passwd'))
        with self.assertRaises(ValueError):
            dc.validate(entry(evaluator='cloud', check={'type': 'local_report'}))
        d = tempfile.mkdtemp()
        Path(d, 'a.json').write_text(json.dumps(entry()))
        self.assertEqual(len(dc.load_registry(d)), 1)

    def test_shipped_registry_is_valid(self):
        for e in dc.load_registry():
            self.assertTrue(e['row'])


if __name__ == '__main__':
    unittest.main()
