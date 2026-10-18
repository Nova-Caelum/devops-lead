#!/usr/bin/env python3
"""Deferred checks: verdicts for rows whose evidence lands after the work, plus a cross-watch safety net.

Each file in deferred_checks/*.json declares one row, the evidence that closes it, a due time and a grace.
This cloud half evaluates the checks it can see from GitHub, posts one worklog verdict per row
(tags deferred-check + pass|fail + the row), and raises Telegram alarms when:
  (a) the Mac closer's heartbeat is older than HEARTBEAT_MAX_HOURS,
  (b) a PASS verdict has sat unapplied (row still open) longer than UNAPPLIED_MAX_HOURS,
  (c) a registered row is past its due time plus grace and still open,
  plus a FAIL verdict once the check is due.
The Mac closer (canonical _agentOS/system/bin/deferred_check_closer.py) re-verifies the evidence,
writes the evidence file and closes the row through the verifier. This half never closes a row.

This repository is public: stdout carries counts and failure classes only. Details go to the private
worklog and to Daniel's Telegram.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

OPS = 'https://nova-caelum-ops-production.up.railway.app/mcp'
TERMINAL = {'done', 'archived'}
HEARTBEAT_MAX_HOURS = 48
UNAPPLIED_MAX_HOURS = 24
REGISTRY_DIR = Path(__file__).resolve().parents[1] / 'deferred_checks'
CLOUD_TYPES = {'github_workflow_run', 'escalation_rate_drop'}


class GhError(Exception):
    pass


class OpsError(Exception):
    pass


class TelegramError(Exception):
    pass


# ── transports (injected in tests) ───────────────────────────────────────────
def run_gh(args):
    """Run `gh`; the error carries the HTTP status only, never stderr (public CI logs)."""
    try:
        return subprocess.run(['gh', *args], check=True, capture_output=True, text=True, timeout=60).stdout
    except subprocess.CalledProcessError as e:
        status = re.search(r'HTTP (\d{3})', e.stderr or '')
        raise GhError(f'HTTP {status.group(1)}' if status else 'gh failed') from None
    except subprocess.TimeoutExpired:
        raise GhError('gh timeout') from None


def gh_api(gh, path):
    return json.loads(gh(['api', path]))


def make_rpc(token):
    def rpc(name, args):
        if not token:
            raise OpsError('missing credential')
        body = {'jsonrpc': '2.0', 'id': 'deferred-checks', 'method': 'tools/call',
                'params': {'name': name, 'arguments': args}}
        req = urllib.request.Request(OPS, data=json.dumps(body).encode(), headers={
            'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
            'Authorization': 'Bearer ' + token})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                outer = json.load(resp)
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise OpsError('transport: ' + name) from None
        result = outer.get('result', {})
        if outer.get('error') or result.get('isError'):
            raise OpsError('refused: ' + name)
        return json.loads(result['content'][0]['text'])
    return rpc


def send_telegram(token, chat_id, text):
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{token}/sendMessage',
        data=json.dumps({'chat_id': chat_id, 'text': text, 'link_preview_options': {'is_disabled': True}}).encode(),
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        raise TelegramError(f'HTTP {e.code}') from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise TelegramError('transport') from None
    if not body.get('ok'):
        raise TelegramError('not ok')
    return (body.get('result') or {}).get('message_id')


# ── registry ─────────────────────────────────────────────────────────────────
REQUIRED = ('row', 'project', 'external_id', 'evaluator', 'check', 'evidence_path', 'evidence_line', 'due', 'grace_hours')


def parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def validate(entry):
    missing = [k for k in REQUIRED if k not in entry]
    if missing:
        raise ValueError('registry entry missing ' + ','.join(missing))
    if entry['evaluator'] not in ('cloud', 'mac'):
        raise ValueError('evaluator must be cloud or mac')
    if entry['evaluator'] == 'cloud' and entry['check'].get('type') not in CLOUD_TYPES:
        raise ValueError('cloud evaluator cannot run check type ' + str(entry['check'].get('type')))
    if not entry['evidence_path'].startswith('AgentSecretBase/_ops/deferred-checks/evidence/'):
        raise ValueError('evidence_path must live under AgentSecretBase/_ops/deferred-checks/evidence/')
    parse_time(entry['due'])
    return entry


def load_registry(directory=REGISTRY_DIR):
    return [validate(json.loads(p.read_text())) for p in sorted(Path(directory).glob('*.json'))]


# ── evidence ─────────────────────────────────────────────────────────────────
def eval_github_workflow_run(gh, check):
    """Newest qualifying run: event, conclusion, created after the floor, and (optionally) a named job succeeded."""
    floor = parse_time(check['created_after'])
    runs = gh_api(gh, f"repos/{check['repo']}/actions/workflows/{check['workflow']}/runs"
                      f"?event={check['event']}&per_page=20").get('workflow_runs', [])
    for run in runs:
        if parse_time(run['created_at']) < floor or run.get('status') != 'completed':
            continue
        if run.get('conclusion') != check['conclusion']:
            continue
        if check.get('job'):
            jobs = gh_api(gh, f"repos/{check['repo']}/actions/runs/{run['id']}/jobs").get('jobs', [])
            if not any(j.get('name') == check['job'] and j.get('conclusion') == 'success' for j in jobs):
                continue
        return 'pass', {'run_id': run['id'], 'run_url': run['html_url'], 'event': run['event'],
                        'conclusion': run['conclusion'], 'created_at': run['created_at']}
    return 'fail', {'reason': f"no {check['event']} run of {check['workflow']} with conclusion "
                              f"{check['conclusion']} since {check['created_after']}"}


def count_prs(gh, repos, label, start, end):
    """One combined query per window (search API allows 30/min; per-repo queries blew it)."""
    scope = " ".join("repo:" + r for r in repos)
    q = f"is:pr created:{start}..{end} {scope}"
    total = gh_api(gh, "search/issues?q=" + q.replace(" ", "+") + "&per_page=1").get("total_count", 0)
    labelled = gh_api(gh, "search/issues?q=" + (q + f" label:{label}").replace(" ", "+") + "&per_page=1").get("total_count", 0)
    return total, labelled


def eval_escalation_rate_drop(gh, check):
    """Share of PRs carrying the escalation label: after-window must be strictly lower than the baseline."""
    b_total, b_lab = count_prs(gh, check['repos'], check['label'], *check['baseline'])
    a_total, a_lab = count_prs(gh, check['repos'], check['label'], *check['after'])
    if not (b_total and a_total):
        return 'fail', {'reason': f'too few PRs to compare (baseline {b_total}, after {a_total})'}
    b_rate, a_rate = b_lab / b_total, a_lab / a_total
    ev = {'baseline': f'{b_lab}/{b_total}', 'after': f'{a_lab}/{a_total}',
          'baseline_rate': round(b_rate, 3), 'after_rate': round(a_rate, 3)}
    return ('pass' if a_rate < b_rate else 'fail'), ev


EVALUATORS = {'github_workflow_run': eval_github_workflow_run, 'escalation_rate_drop': eval_escalation_rate_drop}


# ── state from ops ───────────────────────────────────────────────────────────
def row_state(read_rpc, entry):
    rows = read_rpc('list_work_items', {'project': entry['project'], 'limit': 500})
    for r in rows:
        if r.get('external_id') == entry['external_id']:
            return r.get('state')
    return None


def search(ops_rpc, tags, since):
    return ops_rpc('search_worklog', {'tags': tags, 'from_date': since.isoformat()}) or []


def tag_for(row):
    return 'row-' + re.sub(r'[^a-z0-9-]+', '-', row.lower()).strip('-')


# ── the run ──────────────────────────────────────────────────────────────────
def run(registry, *, gh, read_rpc, ops_rpc, send, now, simulate_stale_heartbeat=False, only=None, force_due=False, out=print):
    counts = {'checked': 0, 'pass': 0, 'fail': 0, 'pending': 0, 'skipped_done': 0, 'alarms': 0, 'errors': 0}
    today = now.date().isoformat()
    alarms_today = {e.get('summary', '') for e in search(ops_rpc, ['deferred-check-alarm'], now - timedelta(hours=now.hour, minutes=now.minute))}

    def alarm(key, text):
        marker = f'deferred-check alarm [{key}] {today}'
        if any(marker in s for s in alarms_today):
            return
        try:
            send('Deferred checks: ' + text)
        except TelegramError:
            counts['errors'] += 1
            return
        ops_rpc('append_worklog', {'author': 'devops-lead', 'project': 'governance-evolution',
                                   'summary': (marker + ': ' + text)[:280],
                                   'tags': ['deferred-check-alarm', 'deferred-check']})
        alarms_today.add(marker)
        counts['alarms'] += 1

    # (a) heartbeat cross-watch
    beats = search(ops_rpc, ['deferred-check-heartbeat'], now - timedelta(hours=HEARTBEAT_MAX_HOURS))
    if simulate_stale_heartbeat or not beats:
        alarm('heartbeat', f'the Mac closer has posted no heartbeat in {HEARTBEAT_MAX_HOURS}h. '
                           'Rows will not close until it runs (launchd com.novacaelum.deferred-check-closer).'
                           + (' [SIMULATED test alarm]' if simulate_stale_heartbeat else ''))

    for entry in registry:
        if only and entry['row'] != only:
            continue
        counts['checked'] += 1
        state = row_state(read_rpc, entry)
        if state in TERMINAL:
            counts['skipped_done'] += 1
            continue
        due = parse_time(entry['due'])
        row_tag = tag_for(entry['row'])
        # search_worklog matches ANY tag, so search on the row tag alone and keep verdict entries only.
        prior = [p for p in search(ops_rpc, [row_tag], now - timedelta(days=30))
                 if 'deferred-check' in (p.get('tags') or [])]
        prior_pass = [p for p in prior if 'pass' in (p.get('tags') or [])]

        # (b) unapplied PASS
        if prior_pass:
            first = min(parse_time(p['created_at']) for p in prior_pass)
            if now - first > timedelta(hours=UNAPPLIED_MAX_HOURS):
                alarm(f"unapplied {entry['row']}", f"{entry['row']} has a PASS verdict from {first:%Y-%m-%d %H:%MZ} "
                                                   f'but is still {state}. The Mac closer has not applied it.')
        # (c) overdue
        if now > due + timedelta(hours=entry['grace_hours']):
            alarm(f"overdue {entry['row']}", f"{entry['row']} ({entry['external_id']}) was due {entry['due']} "
                                             f'and is still {state}.')

        if entry['evaluator'] != 'cloud' or prior_pass:
            continue
        verdict, ev = EVALUATORS[entry['check']['type']](gh, entry['check'])
        if verdict == 'fail' and now < due and not force_due:
            counts['pending'] += 1
            continue
        counts[verdict] += 1
        already = [p for p in prior if verdict in (p.get('tags') or [])
                   and parse_time(p['created_at']).date().isoformat() == today]
        if already:
            continue
        summary = f"deferred-check {verdict.upper()} {entry['row']} ({entry['external_id']})"
        result = ops_rpc('append_worklog', {
            'author': 'devops-lead', 'project': entry['project'], 'summary': summary[:280],
            'detailed': json.dumps({'row': entry['row'], 'external_id': entry['external_id'], 'verdict': verdict,
                                    'evidence': ev, 'check': entry['check']}, ensure_ascii=False),
            'tags': ['deferred-check', verdict, row_tag]})
        uuid.UUID(result['id'])
        if verdict == 'fail':
            alarm(f"fail {entry['row']}", f"{entry['row']} FAIL: {ev.get('reason', ev)}")
    out(' '.join(f'{k}={v}' for k, v in counts.items()))
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--only', help='evaluate one registered row (short ID)')
    ap.add_argument('--simulate-stale-heartbeat', action='store_true', help='raise the heartbeat alarm (safety-net test)')
    ap.add_argument('--now', help='ISO time to evaluate at (tests only)')
    ap.add_argument('--force-due', action='store_true', help='judge checks as due now (proof runs); dates stay real')
    args = ap.parse_args(argv)
    env = os.environ
    token, chat = env.get('TELEGRAM_BOT_TOKEN'), env.get('TELEGRAM_DANIEL_CHAT_ID')
    if not (token and chat and env.get('OPS_SERVER_TOKEN') and env.get('STALE_SWEEP_READ_TOKEN')):
        print('ERROR: missing secret(s)')
        return 2
    try:
        run(load_registry(), gh=run_gh, read_rpc=make_rpc(env['STALE_SWEEP_READ_TOKEN']),
            ops_rpc=make_rpc(env['OPS_SERVER_TOKEN']), send=lambda t: send_telegram(token, chat, t),
            now=parse_time(args.now) if args.now else datetime.now(timezone.utc),
            simulate_stale_heartbeat=args.simulate_stale_heartbeat, only=args.only, force_due=args.force_due)
    except (GhError, OpsError, KeyError, ValueError) as e:
        print(f'ERROR: {type(e).__name__}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
