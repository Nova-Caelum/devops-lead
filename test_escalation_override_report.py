import contextlib
import importlib.util
import io
import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('report', Path(__file__).parent / 'scripts/escalation_override_report.py')
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
OLD = '2026-10-19T08:00:00Z'
SHA = '789d53e717c07a1979dc1204c5ab969785ac5046'

ESCALATION = """## DevOps-Lead review — 🔴 Escalation

**Verdict:** Escalate-list items 6 and 4 tripped — Daniel decision required.

**Files touched:** `a.py`, `b.py`.

**What tripped it:**
- Item 6 (gate-weakening, bypass flag): `a.py:10` launches sessions with a bypass flag.
- Item 4 (permission grant the registry does not track): a runtime flag the registry cannot see.

**What would clear it:** Confirmation from Daniel.

CHANGE-SUMMARY: Adds a launcher.
ESCALATION-REASON: A launcher bypasses permissions, so Daniel should confirm the design.
"""


def comment(body, user='someone', at='2026-10-19T07:00:00Z'):
    return {'body': body, 'user': {'login': user}, 'created_at': at}


def make_pull(repo='o/r', number=1, merged_at=OLD, labels=('escalate-daniel',), merger='daniel', title='Add launcher', merged=True):
    return {'number': number, 'title': title, 'html_url': f'https://github.com/{repo}/pull/{number}',
            'merged': merged, 'merged_at': merged_at if merged else None,
            'merged_by': {'login': merger} if merged else None, 'merge_commit_sha': SHA,
            'base': {'repo': {'full_name': repo}}, 'labels': [{'name': n} for n in labels]}


class FakeGH:
    def __init__(self, pulls, comments=None, fail_add=False, fail_remove=False, stale_index=False):
        self.pulls = {(p['base']['repo']['full_name'], p['number']): p for p in pulls}
        self.comment_map = comments or {}
        self.fail_add, self.fail_remove, self.stale_index = fail_add, fail_remove, stale_index
        self.events = []

    def search_merged(self, since):
        self.events.append('search')
        found = []
        for (repo, number), p in self.pulls.items():
            names = {l['name'] for l in p['labels']}
            if 'escalate-daniel' in names and p['merged'] and (self.stale_index or report.REPORTED_LABEL not in names):
                found.append({'repo': repo, 'number': number, 'merged_at': p['merged_at']})
        return found

    def pull(self, repo, number):
        return self.pulls[(repo, number)]

    def comments(self, repo, number):
        return self.comment_map.get((repo, number), [comment(ESCALATION, 'claude[bot]')])

    def add_label(self, repo, number, label):
        self.events.append('claim')
        if self.fail_add:
            raise report.GhError('HTTP 403')
        self.pulls[(repo, number)]['labels'].append({'name': label})

    def remove_label(self, repo, number, label):
        self.events.append('unclaim')
        if self.fail_remove:
            raise report.GhError('HTTP 500')
        p = self.pulls[(repo, number)]
        p['labels'] = [l for l in p['labels'] if l['name'] != label]


class Sender:
    def __init__(self, gh=None, fail=False):
        self.gh, self.fail, self.texts = gh, fail, []

    def __call__(self, text):
        if self.gh is not None:
            self.gh.events.append('send')
        if self.fail:
            raise report.TelegramError('HTTP 500')
        self.texts.append(text)
        return 4242


def run(gh, send, **kw):
    kw.setdefault('now', NOW)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        summary = report.run(gh, send, **kw)
    return summary, out.getvalue()


class ParseTests(unittest.TestCase):
    def test_objection_is_the_verdict_line_and_the_item_lines(self):
        parsed = report.parse_escalation(ESCALATION)
        self.assertEqual(parsed['verdict'], 'Verdict: Escalate-list items 6 and 4 tripped — Daniel decision required.')
        self.assertEqual(len(parsed['items']), 2)
        self.assertTrue(parsed['items'][0].startswith('- Item 6 (gate-weakening, bypass flag): a.py:10'))
        self.assertTrue(parsed['items'][1].startswith('- Item 4 (permission grant'))

    def test_the_latest_escalation_comment_wins(self):
        first = ESCALATION.replace('items 6 and 4', 'item 1')
        picked = report.latest_escalation([comment(first, at='2026-10-19T06:00:00Z'), comment('chatter'),
                                           comment(ESCALATION, at='2026-10-19T07:00:00Z')])
        self.assertIn('items 6 and 4', picked)

    def test_reason_needs_the_exact_prefix_and_the_latest_wins(self):
        self.assertIsNone(report.find_reason([comment('Merging over the DevOps-Lead escalation. The design was authorized.')]))
        self.assertIsNone(report.find_reason([comment('Merged over escalation because:   ')]))
        found = report.find_reason([comment('Merged over escalation because: first take', 'a', '2026-10-19T07:00:00Z'),
                                    comment('Merged over escalation because: Daniel decided it.\nSecond line.', 'b', '2026-10-19T07:05:00Z')])
        self.assertEqual(found, ('b', 'Daniel decided it.\nSecond line.'))

    def test_markdown_markup_is_stripped_for_plain_text(self):
        text = report.build_message(make_pull(), [comment(ESCALATION)])
        self.assertNotIn('**', text)
        self.assertNotIn('`', text)


class MessageTests(unittest.TestCase):
    def test_message_carries_every_required_part(self):
        reason = comment('Merged over escalation because: Daniel authorized this design on 2026-10-15.', 'agent-account')
        text = report.build_message(make_pull(merger='merger-login'), [comment(ESCALATION, 'claude[bot]'), reason])
        for part in ('o/r#1', 'Add launcher', 'https://github.com/o/r/pull/1', f'https://github.com/o/r/commit/{SHA}',
                     'merger-login', '2026-10-19 08:00 UTC', 'Verdict: Escalate-list items 6 and 4 tripped',
                     '- Item 6 (gate-weakening', '- Item 4 (permission grant', 'Daniel authorized this design on 2026-10-15.'):
            self.assertIn(part, text)

    def test_missing_reason_says_so(self):
        self.assertIn('no reason given', report.build_message(make_pull(), [comment(ESCALATION)]))

    def test_without_the_structured_comment_the_marker_line_is_the_objection(self):
        body = '## DevOps-Lead review — 🔴 Escalation\n\nfree text\nESCALATION-REASON: Touches the workflow definition.'
        self.assertIn('Touches the workflow definition.', report.build_message(make_pull(), [comment(body)]))

    def test_without_any_escalation_comment_the_message_still_goes(self):
        text = report.build_message(make_pull(), [])
        self.assertIn('no DevOps-Lead escalation comment found', text)
        self.assertIn('no reason given', text)

    def test_message_fits_telegram_and_marks_truncation(self):
        body = ESCALATION.replace('- Item 4', '- ' + 'long ' * 2000 + '\n- Item 4')
        why = comment('Merged over escalation because: ' + 'because ' * 2000)
        text = report.build_message(make_pull(title='T' * 3000), [comment(body), why])
        self.assertLessEqual(len(text), 4096)
        self.assertIn('truncated', text)
        self.assertIn('https://github.com/o/r/pull/1', text)


class EligibilityTests(unittest.TestCase):
    def check(self, pull, **kw):
        args = dict(now=NOW, since=report.SINCE, min_age=report.MIN_AGE, explicit=False)
        args.update(kw)
        return report.skip_reason(pull, **args)

    def test_a_merged_labelled_unreported_pr_in_window_is_eligible(self):
        self.assertIsNone(self.check(make_pull()))

    def test_each_disqualifier_names_itself(self):
        self.assertEqual(self.check(make_pull(merged=False)), 'not merged')
        self.assertEqual(self.check(make_pull(labels=())), 'no escalate-daniel label')
        self.assertEqual(self.check(make_pull(labels=('escalate-daniel', report.REPORTED_LABEL))), 'already reported')
        self.assertEqual(self.check(make_pull(merged_at='2026-10-17T23:59:59Z')), 'merged before the reporting floor')
        young = (NOW - timedelta(minutes=3)).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.assertEqual(self.check(make_pull(merged_at=young)), 'merged too recently')

    def test_explicit_pr_ignores_floor_and_age_but_never_the_reported_mark(self):
        young = (NOW - timedelta(minutes=3)).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.assertIsNone(self.check(make_pull(merged_at='2026-10-01T00:00:00Z'), explicit=True))
        self.assertIsNone(self.check(make_pull(merged_at=young), explicit=True))
        self.assertEqual(self.check(make_pull(labels=('escalate-daniel', report.REPORTED_LABEL)), explicit=True), 'already reported')

    def test_the_floor_is_the_day_daniel_asked(self):
        self.assertEqual(report.SINCE, '2026-10-18T00:00:00Z')


class ReportOnceTests(unittest.TestCase):
    def test_claim_comes_before_send_and_the_mark_stays(self):
        gh = FakeGH([make_pull()])
        send = Sender(gh)
        summary, _ = run(gh, send)
        self.assertEqual(gh.events, ['search', 'claim', 'send'])
        self.assertEqual((summary.sent, summary.failed), (1, 0))
        self.assertIn(report.REPORTED_LABEL, [l['name'] for l in gh.pulls[('o/r', 1)]['labels']])

    def test_a_second_run_sends_nothing(self):
        gh = FakeGH([make_pull()])
        send = Sender(gh)
        run(gh, send)
        summary, _ = run(gh, send)
        self.assertEqual((len(send.texts), summary.sent), (1, 0))

    def test_a_stale_search_index_cannot_double_send(self):
        gh = FakeGH([make_pull(labels=('escalate-daniel', report.REPORTED_LABEL))], stale_index=True)
        send = Sender(gh)
        summary, _ = run(gh, send)
        self.assertEqual((send.texts, summary.sent, summary.skipped), ([], 0, 1))
        self.assertNotIn('claim', gh.events)

    def test_a_failed_claim_sends_nothing(self):
        gh = FakeGH([make_pull()], fail_add=True)
        send = Sender(gh)
        summary, _ = run(gh, send)
        self.assertEqual((send.texts, summary.sent, summary.failed), ([], 0, 1))
        self.assertNotIn('send', gh.events)

    def test_a_failed_send_gives_the_claim_back(self):
        gh = FakeGH([make_pull()])
        summary, _ = run(gh, Sender(gh, fail=True))
        self.assertEqual(gh.events, ['search', 'claim', 'send', 'unclaim'])
        self.assertEqual((summary.sent, summary.failed), (0, 1))
        self.assertNotIn(report.REPORTED_LABEL, [l['name'] for l in gh.pulls[('o/r', 1)]['labels']])

    def test_a_failed_rollback_is_loud(self):
        gh = FakeGH([make_pull()], fail_remove=True)
        summary, out = run(gh, Sender(gh, fail=True))
        self.assertEqual(summary.lost, ['o/r#1'])
        self.assertIn('ROLLBACK_FAILED', out)
        self.assertTrue(summary.exit_code())

    def test_an_unreadable_pr_fails_loudly_without_stopping_the_others(self):
        gh = FakeGH([make_pull(number=1, merged_at='2026-10-19T01:00:00Z'), make_pull(number=2, merged_at='2026-10-19T02:00:00Z')])
        real_pull = gh.pull

        def pull(repo, number):
            if number == 1:
                raise report.GhError('HTTP 403')
            return real_pull(repo, number)
        gh.pull = pull
        send = Sender(gh)
        summary, out = run(gh, send)
        self.assertEqual((summary.sent, summary.failed, len(send.texts)), (1, 1, 1))
        self.assertIn('FAILED read o/r#1', out)
        self.assertTrue(summary.exit_code())

    def test_cap_per_run_takes_the_oldest_first(self):
        pulls = [make_pull(number=n, merged_at=f'2026-10-19T0{n}:00:00Z') for n in (5, 3, 4, 2)]
        gh = FakeGH(pulls)
        send = Sender(gh)
        summary, _ = run(gh, send, max_per_run=2)
        self.assertEqual(summary.sent, 2)
        reported = sorted(n for (r, n), p in gh.pulls.items() if report.REPORTED_LABEL in [l['name'] for l in p['labels']])
        self.assertEqual(reported, [2, 3])

    def test_explicit_pr_sends_even_before_the_floor(self):
        gh = FakeGH([make_pull(number=207, merged_at='2026-10-16T01:06:49Z')])
        send = Sender(gh)
        summary, _ = run(gh, send, only=('o/r', 207))
        self.assertEqual((summary.sent, 'search' in gh.events), (1, False))

    def test_dry_run_prints_the_message_and_changes_nothing(self):
        gh = FakeGH([make_pull()])
        send = Sender(gh)
        summary, out = run(gh, send, only=('o/r', 1), dry_run=True)
        self.assertEqual((send.texts, gh.events), ([], []))
        self.assertIn('Add launcher', out)
        self.assertIn('no reason given', out)
        self.assertEqual(summary.sent, 0)

    def test_the_hosted_log_never_contains_the_message(self):
        gh = FakeGH([make_pull(title='PRIVATE-TITLE-XYZ')])
        _, out = run(gh, Sender(gh))
        self.assertNotIn('PRIVATE-TITLE-XYZ', out)
        self.assertNotIn('Escalate-list items', out)


class GitHubTests(unittest.TestCase):
    def test_search_query_carries_the_floor_and_skips_reported(self):
        seen = []

        def fake(args):
            seen.append(args)
            return json.dumps({'items': [{'repository_url': 'https://api.github.com/repos/o/r', 'number': 7,
                                          'pull_request': {'merged_at': OLD}}]})
        found = report.GitHub(fake).search_merged('2026-10-18T00:00:00Z')
        q = [a for a in seen[0] if a.startswith('q=')][0]
        for part in ('org:Nova-Caelum', 'is:pr', 'is:merged', 'label:escalate-daniel',
                     '-label:' + report.REPORTED_LABEL, 'merged:>=2026-10-18T00:00:00Z'):
            self.assertIn(part, q)
        self.assertEqual(found, [{'repo': 'o/r', 'number': 7, 'merged_at': OLD}])

    def test_claim_creates_the_label_when_missing_and_tolerates_it_existing(self):
        for exists in (False, True):
            calls = []

            def fake(args):
                calls.append(args[3])
                if exists and len(calls) == 1:
                    raise report.GhError('HTTP 422')
                return ''
            report.GitHub(fake).add_label('o/r', 5, report.REPORTED_LABEL)
            self.assertEqual(calls, ['repos/o/r/labels', 'repos/o/r/issues/5/labels'], msg=f'label exists={exists}')

        def forbidden(args):
            raise report.GhError('HTTP 403')
        with self.assertRaises(report.GhError):
            report.GitHub(forbidden).add_label('o/r', 5, report.REPORTED_LABEL)

    def test_comments_paginate_until_a_short_page(self):
        pages = [json.dumps([{'id': i} for i in range(100)]), json.dumps([{'id': 100}])]
        calls = []

        def fake(args):
            calls.append(args)
            return pages[len(calls) - 1]
        self.assertEqual(len(report.GitHub(fake).comments('o/r', 1)), 101)
        self.assertEqual(len(calls), 2)

    def test_gh_failures_carry_the_status_and_not_the_stderr(self):
        err = report.subprocess.CalledProcessError(1, ['gh'], stderr='gh: Resource not accessible (HTTP 403) token=ghp_SECRET')
        with patch.object(report.subprocess, 'run', side_effect=err):
            with self.assertRaises(report.GhError) as ctx:
                report.run_gh(['api', 'x'])
        self.assertEqual(str(ctx.exception), 'HTTP 403')


class TelegramTests(unittest.TestCase):
    def response(self, payload):
        resp = io.BytesIO(json.dumps(payload).encode())
        resp.__enter__ = lambda s=resp: resp
        resp.__exit__ = lambda *a: False
        return resp

    def test_payload_is_plain_text_to_the_configured_chat(self):
        with patch.object(report.urllib.request, 'urlopen', return_value=self.response({'ok': True, 'result': {'message_id': 99}})) as op:
            mid = report.send_telegram('TOKEN123', '555', 'hello')
        req = op.call_args.args[0]
        body = json.loads(req.data)
        self.assertEqual((mid, body['chat_id'], body['text']), (99, '555', 'hello'))
        self.assertNotIn('parse_mode', body)
        self.assertTrue(req.full_url.endswith('/botTOKEN123/sendMessage'))

    def test_not_ok_raises_without_leaking_the_token(self):
        with patch.object(report.urllib.request, 'urlopen', return_value=self.response({'ok': False, 'error_code': 400})):
            with self.assertRaises(report.TelegramError) as ctx:
                report.send_telegram('TOKEN123', '555', 'hello')
        self.assertNotIn('TOKEN123', str(ctx.exception))

    def test_transport_errors_do_not_leak_the_token(self):
        boom = urllib.error.HTTPError('https://api.telegram.org/botTOKEN123/sendMessage', 401, 'Unauthorized', {}, None)
        with patch.object(report.urllib.request, 'urlopen', side_effect=boom):
            with self.assertRaises(report.TelegramError) as ctx:
                report.send_telegram('TOKEN123', '555', 'hello')
        self.assertNotIn('TOKEN123', str(ctx.exception))
        self.assertIn('401', str(ctx.exception))


class MainTests(unittest.TestCase):
    def main(self, argv, gh, env, send=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = report.main(argv, gh=gh, env=env, sender=send, now=NOW)
        return code, out.getvalue()

    def test_dry_run_needs_no_telegram_secrets(self):
        gh = FakeGH([make_pull(number=207)])
        code, out = self.main(['--pr', 'o/r#207', '--dry-run'], gh, {})
        self.assertEqual(code, 0)
        self.assertIn('Add launcher', out)

    def test_missing_telegram_secrets_fail_before_any_claim(self):
        gh = FakeGH([make_pull()])
        code, _ = self.main([], gh, {'TELEGRAM_BOT_TOKEN': 'x'})
        self.assertNotEqual(code, 0)
        self.assertEqual(gh.events, [])

    def test_a_clean_run_exits_zero_and_prints_counts_only(self):
        gh = FakeGH([make_pull(title='PRIVATE-TITLE-XYZ')])
        code, out = self.main([], gh, {'TELEGRAM_BOT_TOKEN': 'x', 'TELEGRAM_DANIEL_CHAT_ID': '1'}, send=Sender(gh))
        self.assertEqual(code, 0)
        self.assertIn('sent=1', out)
        self.assertNotIn('PRIVATE-TITLE-XYZ', out)

    def test_pr_reference_forms(self):
        self.assertEqual(report.parse_ref('Nova-Caelum/_agentOS#207'), ('Nova-Caelum/_agentOS', 207))
        self.assertEqual(report.parse_ref('Nova-Caelum/_agentOS/207'), ('Nova-Caelum/_agentOS', 207))
        with self.assertRaises(ValueError):
            report.parse_ref('207')


if __name__ == '__main__':
    unittest.main()
