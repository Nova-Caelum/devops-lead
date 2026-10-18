#!/usr/bin/env python3
"""Tell Daniel on Telegram, once, about every PR merged while it carried the escalate-daniel label.

The label is a soft gate: a PR can be merged over it. This job never blocks anything. It finds merged PRs that still
carry the label, builds one message per PR (what the reviewer objected to, who merged it, and the reason the merger gave),
and sends it. Report-once state lives on the PR itself as the `escalation-override-reported` label.

Order is claim, send, and give the claim back if the send fails. A token that cannot write labels therefore fails on the
first claim and sends nothing, instead of re-sending the same PRs on every run.

This repository is public: a hosted run prints counts and failure classes only, never a title or a message body.
Only --dry-run prints a message, and it is for local use.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

ORG = 'Nova-Caelum'
ESCALATE_LABEL = 'escalate-daniel'
REPORTED_LABEL = 'escalation-override-reported'
REPORTED_COLOR, REPORTED_DESCRIPTION = '5319E7', 'Merge over an escalation was reported to Daniel'
SINCE = '2026-10-18T00:00:00Z'  # the day Daniel asked for this; 270 older merged PRs carry the label and are not reported
MIN_AGE = timedelta(minutes=10)  # lets a merger post the reason and the search index settle
MAX_PER_RUN = 10
REASON_PREFIX = 'Merged over escalation because:'
NO_REASON = 'no reason given'
ESCALATION_HEADING = re.compile(r'^##\s+DevOps-Lead review\b[^\n]*Escalation', re.I)


class GhError(Exception):
    pass


class TelegramError(Exception):
    pass


def run_gh(args):
    """Run `gh`; the error carries the HTTP status only, never stderr (public CI logs)."""
    try:
        return subprocess.run(['gh', *args], check=True, capture_output=True, text=True, timeout=60).stdout
    except subprocess.CalledProcessError as e:
        status = re.search(r'HTTP (\d{3})', e.stderr or '')
        raise GhError(f'HTTP {status.group(1)}' if status else 'gh failed') from None
    except subprocess.TimeoutExpired:
        raise GhError('gh timeout') from None


class GitHub:
    def __init__(self, runner=run_gh):
        self.gh = runner

    def api(self, path, *fields, method='GET'):
        out = self.gh(['api', '-X', method, path, *[a for f in fields for a in ('-f', f)]])
        return json.loads(out) if out.strip() else None

    def search_merged(self, since):
        q = f'org:{ORG} is:pr is:merged label:{ESCALATE_LABEL} -label:{REPORTED_LABEL} merged:>={since}'
        data = self.api('search/issues', f'q={q}', 'per_page=100', 'sort=updated', 'order=asc')
        return [{'repo': i['repository_url'].rsplit('/repos/', 1)[1], 'number': i['number'],
                 'merged_at': (i.get('pull_request') or {}).get('merged_at')} for i in data['items']]

    def pull(self, repo, number):
        return self.api(f'repos/{repo}/pulls/{number}')

    def comments(self, repo, number):
        found, page = [], 1
        while True:
            batch = self.api(f'repos/{repo}/issues/{number}/comments', 'per_page=100', f'page={page}')
            found += batch
            if len(batch) < 100:
                return found
            page += 1

    def add_label(self, repo, number, label):
        try:
            self.api(f'repos/{repo}/labels', f'name={label}', f'color={REPORTED_COLOR}',
                     f'description={REPORTED_DESCRIPTION}', method='POST')
        except GhError as e:
            if str(e) != 'HTTP 422':  # 422 = the label already exists
                raise
        self.api(f'repos/{repo}/issues/{number}/labels', f'labels[]={label}', method='POST')

    def remove_label(self, repo, number, label):
        self.api(f'repos/{repo}/issues/{number}/labels/{label}', method='DELETE')


def send_telegram(token, chat_id, text):
    """Send plain text (no parse_mode: PR text is untrusted) and return the message id. Errors never carry the URL."""
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
        raise TelegramError(f'not ok ({body.get("error_code", "?")})')
    return (body.get('result') or {}).get('message_id')


def parse_when(value):
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)


def plain(text):
    return text.replace('**', '').replace('`', '')


def clip(text, limit):
    return text if len(text) <= limit else text[:limit - 28].rstrip() + ' … [truncated, see the PR]'


def latest_escalation(comments):
    hits = [c for c in comments if ESCALATION_HEADING.match((c.get('body') or '').lstrip())]
    return max(hits, key=lambda c: c.get('created_at', ''))['body'] if hits else None


def parse_escalation(body):
    """The verdict line and the item lines under 'What tripped it'; falls back to the ESCALATION-REASON marker."""
    lines = body.splitlines()
    verdict = next((plain(l).strip() for l in lines if l.strip().startswith('**Verdict:**')), None)
    items, collecting = [], False
    for line in lines:
        if re.match(r'\*\*What tripped it', line.strip()):
            collecting = True
        elif collecting:
            stripped = line.strip()
            if stripped.startswith('**'):
                break
            if not stripped:
                if items:
                    break
            elif stripped.startswith('- '):
                items.append(plain(stripped))
            elif items:
                items[-1] += ' ' + plain(stripped)
    marker = next((l.split(':', 1)[1].strip() for l in lines if l.startswith('ESCALATION-REASON:')), None)
    return {'verdict': verdict, 'items': items, 'marker': marker}


def find_reason(comments):
    """(author, text) of the latest comment that starts with the agreed prefix and says something, else None."""
    for c in sorted(comments, key=lambda c: c.get('created_at', ''), reverse=True):
        body = (c.get('body') or '').strip()
        if body.lower().startswith(REASON_PREFIX.lower()):
            text = body[len(REASON_PREFIX):].strip()
            if text:
                return (c.get('user') or {}).get('login', '?'), text
    return None


def build_message(pull, comments):
    repo, number = pull['base']['repo']['full_name'], pull['number']
    merged = parse_when(pull['merged_at']).strftime('%Y-%m-%d %H:%M UTC')
    body = latest_escalation(comments)
    parsed = parse_escalation(body) if body else None
    if parsed and (parsed['verdict'] or parsed['items']):
        objection = '\n'.join(([parsed['verdict']] if parsed['verdict'] else []) + parsed['items'])
    elif parsed and parsed['marker']:
        objection = plain(parsed['marker'])
    else:
        objection = 'no DevOps-Lead escalation comment found (the label was applied without a review comment); see the PR'
    reason = find_reason(comments)
    why = f'{reason[1]}\n(comment by {reason[0]})' if reason else NO_REASON
    head = '\n'.join([
        'Escalation override: merged over the escalate-daniel label', '',
        f'{repo}#{number}  {clip(pull["title"], 200)}',
        f'Merged by {(pull.get("merged_by") or {}).get("login", "unknown")} at {merged}',
        f'PR: {pull["html_url"]}',
        f'Merge commit: https://github.com/{repo}/commit/{pull["merge_commit_sha"]}'])
    # Worst case is about 3.7k characters, inside Telegram's 4096 limit, so the section budgets are the only guard.
    return f'{head}\n\nOBJECTION (DevOps-Lead)\n{clip(objection, 2000)}\n\nWHY IT WAS NEEDED\n{clip(why, 900)}'


def skip_reason(pull, *, now, since, min_age, explicit):
    """Why this PR is not reported now, or None. An explicit PR ignores the floor and the age, never the reported mark."""
    names = {l['name'] for l in pull.get('labels', [])}
    if not pull.get('merged'):
        return 'not merged'
    if ESCALATE_LABEL not in names:
        return f'no {ESCALATE_LABEL} label'
    if REPORTED_LABEL in names:
        return 'already reported'
    if not explicit:
        merged_at = parse_when(pull['merged_at'])
        if merged_at < parse_when(since):
            return 'merged before the reporting floor'
        if now - merged_at < min_age:
            return 'merged too recently'
    return None


@dataclass
class Summary:
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    lost: list = field(default_factory=list)  # claimed, send failed, claim could not be given back: not delivered

    def exit_code(self):
        return 1 if self.failed or self.lost else 0


def report_pr(gh, send, repo, number, *, now, since, min_age, explicit, dry_run, summary, out):
    try:
        pull = gh.pull(repo, number)
        why = skip_reason(pull, now=now, since=since, min_age=min_age, explicit=explicit)
        if why:
            summary.skipped += 1
            if explicit:
                out(f'skipped {repo}#{number}: {why}')
            return False
        message = build_message(pull, gh.comments(repo, number))
    except GhError as e:
        summary.failed += 1
        out(f'FAILED read {repo}#{number}: GhError {e}')
        return True
    if dry_run:
        out(message)
        return False
    try:
        gh.add_label(repo, number, REPORTED_LABEL)
    except GhError as e:
        summary.failed += 1
        out(f'FAILED claim {repo}#{number}: GhError {e}')
        return True
    try:
        message_id = send(message)
    except Exception as e:
        summary.failed += 1
        out(f'FAILED send {repo}#{number}: {type(e).__name__}{" " + str(e) if isinstance(e, TelegramError) else ""}')
        try:
            gh.remove_label(repo, number, REPORTED_LABEL)
        except GhError:
            summary.lost.append(f'{repo}#{number}')
            out(f'ROLLBACK_FAILED {repo}#{number}: the message was NOT delivered; remove the {REPORTED_LABEL} label to retry')
        return True
    summary.sent += 1
    if explicit:
        out(f'sent {repo}#{number}: telegram message_id={message_id}')
    return True


def run(gh, send, *, now, since=SINCE, min_age=MIN_AGE, max_per_run=MAX_PER_RUN, only=None, dry_run=False, out=print):
    summary = Summary()
    if only:
        refs = [only]
    else:
        found = sorted(gh.search_merged(since), key=lambda f: f['merged_at'] or '')
        refs = [(f['repo'], f['number']) for f in found]
    attempted = 0
    for repo, number in refs:
        if attempted >= max_per_run:
            break
        if report_pr(gh, send, repo, number, now=now, since=since, min_age=min_age, explicit=bool(only),
                     dry_run=dry_run, summary=summary, out=out):
            attempted += 1
    return summary


def parse_ref(value):
    m = re.fullmatch(r'([\w.-]+/[\w.-]+)[#/](\d+)', value)
    if not m:
        raise ValueError('expected OWNER/REPO#NUMBER')
    return m.group(1), int(m.group(2))


def main(argv=None, *, gh=None, env=None, sender=None, now=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--pr', type=parse_ref, metavar='OWNER/REPO#N', help='report this PR only; ignores the floor and the age')
    ap.add_argument('--dry-run', action='store_true', help='print the message; send and mark nothing (local use only)')
    args = ap.parse_args(argv)
    env = os.environ if env is None else env
    token, chat = env.get('TELEGRAM_BOT_TOKEN'), env.get('TELEGRAM_DANIEL_CHAT_ID')
    if not args.dry_run and sender is None:
        if not (token and chat):
            print('ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_DANIEL_CHAT_ID must both be set')
            return 2
        sender = lambda text: send_telegram(token, chat, text)
    try:
        summary = run(gh or GitHub(), sender, now=now or datetime.now(timezone.utc), only=args.pr, dry_run=args.dry_run)
    except (GhError, KeyError, ValueError) as e:
        print(f'ERROR: {type(e).__name__} {e if isinstance(e, GhError) else ""}'.strip())
        return 1
    print(f'sent={summary.sent} skipped={summary.skipped} failed={summary.failed}')
    return summary.exit_code()


if __name__ == '__main__':
    sys.exit(main())
