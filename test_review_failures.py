"""Execute failure/bookkeeping shell from the workflow with offline service fakes."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

WORKFLOW = Path(__file__).parent / '.github/workflows/devops-lead-pr-review.yml'


def run_block(name):
    text = WORKFLOW.read_text()
    step = text.split('      - name: ' + name, 1)[1].split('      - name:', 1)[0]
    lines = step.split('        run: |\n', 1)[1].splitlines()
    block = []
    for line in lines:
        if line and not line.startswith('          '):
            break
        block.append(line[10:])
    return '\n'.join(block)


class FailureLabels(unittest.TestCase):
    def exercise(self, outcome, output=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / 'gh.log'
            (root / 'gh').write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$GH_TEST_LOG"\n')
            response = json.dumps({'result': {'content': [{'text': json.dumps({'id': '12345678-1234-1234-1234-123456789abc'})}]}})
            (root / 'curl').write_text('#!/bin/sh\ncat <<\'RESPONSE\'\n' + response + '\nRESPONSE\n')
            for name in ('gh', 'curl'):
                (root / name).chmod(0o755)
            if output is not None:
                (root / 'claude-execution-output.json').write_text(json.dumps(output))
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                       RUNNER_TEMP=str(root), GH_TEST_LOG=str(log), PR='42',
                       RUN_URL='https://example.invalid/run/1', REVIEW_OUTCOME=outcome,
                       OPS_SERVER_TOKEN='offline-test')
            replacements = {'github.event.pull_request.number': '42', 'github.event.repository.name': 'test-repo',
                            'github.run_id': '1', 'steps.review.outcome': outcome, 'inputs.worklog_project': 'test-project'}
            names = ['Record review outcome']
            if outcome != 'success':
                names.insert(0, 'Loud failure')
            outputs = []
            for name in names:
                block = re.sub(r'\$\{\{\s*(.*?)\s*\}\}', lambda m: replacements[m[1]], run_block(name))
                result = subprocess.run(['bash', '-c', block], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs.append(result.stdout)
            return log.read_text(), '\n'.join(outputs)

    def test_setup_failure_uses_failure_label_only(self):
        calls, _ = self.exercise('skipped')
        self.assertIn('--add-label review-failed', calls)
        self.assertNotIn('--add-label escalate-daniel', calls)
        self.assertIn('setup step failed', calls)

    def test_startup_failure_uses_failure_label_only(self):
        calls, _ = self.exercise('failure')
        self.assertIn('--add-label review-failed', calls)
        self.assertNotIn('--add-label escalate-daniel', calls)
        self.assertIn('failed before Claude produced', calls)

    def test_rate_limit_is_sanitized_and_loud(self):
        calls, output = self.exercise('failure', {'error': '429 secret-like-canary'})
        self.assertIn('rate limit (429)', calls)
        self.assertIn('--add-label review-failed', calls)
        self.assertNotIn('--add-label escalate-daniel', calls)
        self.assertNotIn('secret-like-canary', calls + output)

    def test_successful_validation_skip_still_escalates(self):
        calls, _ = self.exercise('success')
        self.assertIn('--add-label escalate-daniel', calls)
        self.assertNotIn('--add-label review-failed', calls)

    def test_successful_review_adds_neither_label(self):
        calls, _ = self.exercise('success', {'result': 'reviewed'})
        self.assertNotIn('--add-label', calls)


if __name__ == '__main__':
    unittest.main()
