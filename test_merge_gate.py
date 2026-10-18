"""Run the merge gate embedded in the reusable workflow against an offline fake `gh`.

GOV-379: every `_agentOS` review ended `GATE=refused - CI state unreadable`. The gate sent the
Actions API's stderr to /dev/null, so a permission denial, a rate limit, a server error and a
malformed response all read the same. These tests pin the cases apart.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

WORKFLOW = Path(__file__).parent / '.github/workflows/devops-lead-pr-review.yml'
SHA = 'a' * 40
SELF_RUN = 900
REPO = 'Nova-Caelum/example'

FAKE_GH = r'''#!/bin/bash
echo "$*" >> "$GH_TEST_LOG"
case "$1" in
  pr)
    case "$2" in
      view)
        case "$*" in
          *autoMergeRequest*) echo "${FAKE_AFTER_MERGE:-MERGED none}" ;;
          *) echo "OPEN ${FAKE_HEAD} ${FAKE_LABELS:-}" ;;
        esac ;;
      merge) exit "${FAKE_MERGE_RC:-0}" ;;
    esac ;;
  api)
    n=$(( $(cat "$FAKE_DIR/count" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$FAKE_DIR/count"
    last=$(ls "$FAKE_DIR"/step.*.rc | wc -l | tr -d ' ')
    (( n > last )) && n=$last
    [[ -f "$FAKE_DIR/step.$n.out" ]] && cat "$FAKE_DIR/step.$n.out"
    [[ -f "$FAKE_DIR/step.$n.err" ]] && cat "$FAKE_DIR/step.$n.err" >&2
    exit "$(cat "$FAKE_DIR/step.$n.rc")" ;;
esac
'''


def gate_script():
    text = WORKFLOW.read_text()
    body = text.split("cat > \"$GATE\" <<'GATE_EOF'\n", 1)[1].split('\n          GATE_EOF', 1)[0]
    return '\n'.join(line[10:] for line in body.splitlines()) + '\n'


def run_record(run_id, name='CI', status='completed', conclusion='success', path='.github/workflows/ci.yml',
               event='pull_request'):
    return {'id': run_id, 'name': name, 'status': status, 'conclusion': conclusion, 'path': path, 'event': event}


def runs_body(*records):
    return json.dumps({'total_count': len(records), 'workflow_runs': list(records)})


REVIEW_ONLY = runs_body(run_record(SELF_RUN, 'DevOps-Lead PR Review', 'in_progress', None,
                                   '.github/workflows/devops-lead-pr-review.yml'))


def ok(body):
    return {'out': body, 'err': '', 'rc': 0}


def fail(err, rc=1):
    return {'out': '', 'err': err, 'rc': rc}


class MergeGate(unittest.TestCase):
    def run_gate(self, steps, labels='', merge_rc=0, after_merge='MERGED none'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'gh').write_text(FAKE_GH)
            (root / 'gh').chmod(0o755)
            (root / 'gate.sh').write_text(gate_script())
            for number, step in enumerate(steps, 1):
                (root / f'step.{number}.out').write_text(step['out'])
                (root / f'step.{number}.err').write_text(step['err'])
                (root / f'step.{number}.rc').write_text(str(step['rc']))
            log = root / 'gh.log'
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'], GH_TEST_LOG=str(log),
                       FAKE_DIR=str(root), FAKE_HEAD=SHA, FAKE_LABELS=labels, FAKE_MERGE_RC=str(merge_rc),
                       FAKE_AFTER_MERGE=after_merge, DEVOPS_LEAD_GATE_POLL_SEC='0', DEVOPS_LEAD_CI_WAIT_MINUTES='1')
            result = subprocess.run(['bash', str(root / 'gate.sh'), REPO, '7', SHA, str(SELF_RUN)], env=env,
                                    capture_output=True, text=True, timeout=60)
            calls = log.read_text() if log.exists() else ''
            lines = result.stdout.strip().splitlines()
            return result.returncode, lines[-1] if lines else '', result.stdout, calls

    # --- the gate's CI reading: cases that must merge -------------------------------------------

    def test_no_ci_configured_merges_and_says_so(self):
        rc, last, out, calls = self.run_gate([ok(REVIEW_ONLY)])
        self.assertEqual((rc, last), (0, 'GATE=merged'), out)
        self.assertIn('pr merge 7', calls)
        self.assertIn('no CI configured', out)

    def test_green_ci_merges_and_counts_the_runs(self):
        rc, last, out, calls = self.run_gate([ok(runs_body(run_record(SELF_RUN, 'DevOps-Lead PR Review', 'in_progress', None,
                                                                      '.github/workflows/devops-lead-pr-review.yml'),
                                                           run_record(1)))])
        self.assertEqual((rc, last), (0, 'GATE=merged'), out)
        self.assertIn('CI complete and green', out)
        self.assertIn('1 other workflow run(s) checked', out)
        self.assertNotIn('no CI configured', out)

    def test_pending_ci_is_waited_for(self):
        pending = ok(runs_body(run_record(1, status='in_progress', conclusion=None)))
        done = ok(runs_body(run_record(1)))
        rc, last, out, calls = self.run_gate([pending, done])
        self.assertEqual((rc, last), (0, 'GATE=merged'), out)
        self.assertIn('waiting on CI', out)

    # --- cases that must refuse -----------------------------------------------------------------

    def test_failed_ci_refuses_and_names_the_run(self):
        rc, last, out, calls = self.run_gate([ok(runs_body(run_record(1, 'CI', conclusion='failure')))])
        self.assertEqual(rc, 3)
        self.assertEqual(last, 'GATE=refused - CI failed: CI=failure')
        self.assertNotIn('pr merge', calls)

    def test_escalate_label_refuses_before_reading_ci(self):
        rc, last, out, calls = self.run_gate([ok(REVIEW_ONLY)], labels='escalate-daniel')
        self.assertEqual((rc, last), (3, 'GATE=refused - PR is labelled escalate-daniel'))
        self.assertNotIn('actions/runs', calls)

    # --- a real API error still fails closed, and says which kind -------------------------------

    def assert_unreadable(self, steps, reason):
        rc, last, out, calls = self.run_gate(steps)
        self.assertEqual(rc, 3, out)
        self.assertTrue(last.startswith('GATE=refused - CI state unreadable'), last)
        self.assertIn(reason, last)
        self.assertNotIn('pr merge', calls)
        return last

    def test_permission_denied_names_the_missing_permission(self):
        denied = fail('gh: Resource not accessible by integration (HTTP 403)')
        last = self.assert_unreadable([denied], 'actions: read')
        self.assertIn('denied', last)

    def test_hidden_repo_404_is_treated_as_permission(self):
        self.assert_unreadable([fail('gh: Not Found (HTTP 404)')], 'actions: read')

    def test_rate_limit_is_its_own_class(self):
        last = self.assert_unreadable([fail('gh: API rate limit exceeded (HTTP 429)')], 'rate-limited')
        self.assertNotIn('actions: read', last)

    def test_server_error_is_its_own_class(self):
        last = self.assert_unreadable([fail('gh: Bad Gateway (HTTP 502)')], 'server or network error')
        self.assertNotIn('actions: read', last)

    def test_unparseable_response_is_its_own_class(self):
        last = self.assert_unreadable([ok('<html>not json</html>')], 'could not be parsed')
        self.assertNotIn('actions: read', last)

    def test_oversize_response_is_unparseable_not_silently_merged(self):
        body = json.dumps({'total_count': 101, 'workflow_runs': [run_record(1)]})
        self.assert_unreadable([ok(body)], 'could not be parsed')

    def test_unclassified_failure_still_fails_closed(self):
        self.assert_unreadable([fail('gh: something nobody has seen before')], 'unclassified')

    def test_recovers_when_a_retry_succeeds(self):
        rc, last, out, calls = self.run_gate([fail('gh: Bad Gateway (HTTP 502)'), ok(REVIEW_ONLY)])
        self.assertEqual((rc, last), (0, 'GATE=merged'), out)

    def test_raw_api_text_never_reaches_the_gate_line(self):
        canary = 'gh: Resource not accessible by integration (HTTP 403) tok-canary-123'
        rc, last, out, calls = self.run_gate([fail(canary)])
        self.assertNotIn('tok-canary-123', out)
        self.assertNotIn('Resource not accessible', out)


class ReviewStepGrantsActionsRead(unittest.TestCase):
    """The action overwrites GH_TOKEN in the model's shell with an app token minted by an OIDC exchange.
    That token carries contents/pull_requests/issues only, unless the action is asked for more."""

    def test_review_step_requests_actions_read(self):
        text = WORKFLOW.read_text()
        step = text.split('      - name: DevOps-Lead review', 1)[1].split('\n      - name:', 1)[0]
        match = re.search(r'^ {10}additional_permissions: \|\n((?: {12}\S.*\n)+)', step, re.M)
        self.assertIsNotNone(match, 'review step has no additional_permissions block')
        self.assertIn('actions: read', [line.strip() for line in match[1].splitlines()])

    def test_job_grants_the_permission_it_requests(self):
        text = WORKFLOW.read_text()
        job = text.split('  devops-lead-review:\n', 1)[1].split('    steps:', 1)[0]
        self.assertRegex(job, r'permissions:(?:\n {6}\S.*)*\n {6}actions: read')


if __name__ == '__main__':
    unittest.main()
