#!/usr/bin/env python3
"""Deterministic stale candidates; never close work or rewrite release content."""
import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import base64
import re
import subprocess
from urllib.parse import quote
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request
import uuid

OPS = 'https://nova-caelum-ops-production.up.railway.app/mcp'
TERMINAL = {'done', 'completed', 'closed', 'archived', 'descoped', 'killed'}



def rpc(name, args, token):
    if not token:
        raise ValueError('missing scoped credential')
    body = {'jsonrpc': '2.0', 'id': 'stale-sweep', 'method': 'tools/call',
            'params': {'name': name, 'arguments': args}}
    req = urllib.request.Request(OPS, data=json.dumps(body).encode(), headers={
        'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(req, timeout=20) as resp:
        outer = json.load(resp)
    result = outer.get('result', {})
    if outer.get('error') or result.get('isError'):
        raise ValueError('ops call failed: ' + name)
    return json.loads(result['content'][0]['text'])


def snapshot(token):
    result = {}; gaps = []
    for name in ('list_projects', 'list_modules', 'list_work_items'):
        result[name] = rpc(name, {'limit': 500}, token)
        if len(result[name]) == 500:
            gaps.append(name + ': global response capped; older unassigned rows may be absent')
    for name in ('list_modules', 'list_work_items'):
        rows = {r['id']: r for r in result[name]}
        for project in result['list_projects']:
            batch = rpc(name, {'project': project['code'], 'limit': 500}, token)
            rows.update({r['id']: r for r in batch})
            if len(batch) == 500:
                gaps.append(name + ': project cap ' + project['code'])
        result[name] = list(rows.values())
    result['coverage_gaps'] = gaps
    return result


def timestamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def census(data, now=None, days=14):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    findings = []; gaps = list(data.get('coverage_gaps', [])); counts = {}
    for kind in ('list_projects', 'list_modules', 'list_work_items'):
        rows = data.get(kind, []); counts[kind] = len(rows)
        for row in rows:
            ident = row.get('short_id') or row.get('code') or row.get('external_id') or row['id']
            state = row.get('state', row.get('status'))
            updated = timestamp(row.get('updated_at'))
            if state not in TERMINAL and state not in {'paused', 'deferred'}:
                if updated and updated < cutoff:
                    findings.append({'kind': 'stale-candidate', 'surface': kind, 'id': ident,
                                     'state': state, 'updated_at': row['updated_at']})
                elif updated is None:
                    findings.append({'kind': 'timestamp-missing', 'surface': kind, 'id': ident})
            if kind != 'list_work_items' and state not in TERMINAL:
                if not str(row.get('folder_path') or '').strip():
                    findings.append({'kind': 'folder-missing', 'surface': kind, 'id': ident})
                if kind == 'list_modules' and not row.get('code_repo'):
                    findings.append({'kind': 'code-repo-missing', 'surface': kind, 'id': ident})
    return {'observed_at': now.isoformat(), 'candidate_age_days': days, 'counts': counts,
            'coverage_gaps': gaps, 'findings': findings}


def post_report(report, token):
    kinds = Counter(f['kind'] for f in report['findings'])
    summary = 'daily stale sweep: ' + ', '.join(f'{k}={v}' for k,v in report['counts'].items())
    summary += f"; candidates={len(report['findings'])}; coverage gaps={len(report['coverage_gaps'])}"
    # Bound transmission; full report stays local. Explicitly indicate omitted records.
    detail = {**report, 'finding_counts': dict(kinds), 'findings': report['findings'][:25],
              'omitted_findings': max(0, len(report['findings']) - 25)}
    result = rpc('append_worklog', {'author': 'devops-lead', 'project': 'governance-evolution-sprint',
        'summary': summary[:280], 'detailed': json.dumps(detail, ensure_ascii=False),
        'tags': ['daily-stale-sweep', 'row-22', 'review-candidates']}, token)
    uuid.UUID(result['id']) # A success response without a persisted row ID is not sufficient.



def gh_json(*args):
    proc = subprocess.run(['gh', *args], check=True, capture_output=True, text=True, timeout=45)
    return json.loads(proc.stdout)


def api(path):
    return gh_json('api', path)


def content(repo, path, ref):
    result = api(f'repos/{repo}/contents/{quote(path, safe="/")}?ref={quote(ref, safe="")}')
    return json.loads(base64.b64decode(result['content']))


def catalog_findings(repo, branch, manifest):
    findings = []
    plugins = manifest.get('plugins')
    if not isinstance(plugins, list) or not plugins:
        raise ValueError('catalog has no plugins')
    for plugin in plugins:
        name, source = plugin['name'], plugin['source']
        if isinstance(source, str) and source.startswith('./') and '..' not in source.split('/'):
            path = source[2:].rstrip('/')
            content(repo, (path + '/' if path else '') + '.claude-plugin/plugin.json', branch)
            continue  # Relative source follows this catalog's branch; it has no immutable pin.
        if not isinstance(source, dict):
            findings.append({'kind': 'catalog-source-unverified', 'repo': repo, 'plugin': name})
            continue
        match = re.fullmatch(r'https://github.com/([\w.-]+/[\w.-]+?)(?:\.git)?', source.get('url', ''))
        pin, path = source.get('sha', ''), source.get('path', '').strip('/')
        if not match or source.get('source') != 'git-subdir' or not re.fullmatch(r'[0-9a-fA-F]{40}', pin) or '..' in path.split('/'):
            findings.append({'kind': 'catalog-pin-unverified', 'repo': repo, 'plugin': name})
            continue
        target = match.group(1)
        # Resolve the advertised artifact at the exact pin; never execute it or auto-bump a release.
        content(target, (path + '/' if path else '') + '.claude-plugin/plugin.json', pin)
        default = api(f'repos/{target}')['default_branch']
        comparison = api(f'repos/{target}/compare/{pin}...{quote(default, safe="")}?per_page=1')
        files = comparison.get('files', [])
        prefix = path + '/' if path else ''
        relevant = [f['filename'] for f in files if f['filename'].startswith(prefix) or f.get('previous_filename', '').startswith(prefix)]
        if relevant or comparison.get('status') in {'diverged', 'behind'}:
            findings.append({'kind': 'catalog-pin-review-candidate', 'repo': repo, 'plugin': name,
                             'pin': pin, 'comparison': comparison.get('status'), 'changed_paths': relevant[:10]})
        if len(files) >= 300:
            findings.append({'kind': 'catalog-comparison-truncated', 'repo': repo, 'plugin': name})
    return findings


def github_census(config, now=None, days=14):
    now = now or datetime.now(timezone.utc)
    findings, gaps = [], []
    counts = {'github_repositories': 0, 'open_prs': 0, 'catalogs': 0}
    failed = False
    for entry in config['repositories']:
        repo = entry['repo']
        if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo): raise ValueError('invalid repository')
        try:
            branch = api(f'repos/{repo}')['default_branch']
            prs = gh_json('pr', 'list', '--repo', repo, '--state', 'open', '--limit', '100',
                          '--json', 'number,updatedAt,isDraft,statusCheckRollup,headRefOid')
            counts['github_repositories'] += 1
            counts['open_prs'] += len(prs)
            if len(prs) == 100: gaps.append(repo + ': open PR list capped at 100')
            for pr in prs:
                updated = timestamp(pr['updatedAt'])
                if updated and updated < now - timedelta(days=days):
                    findings.append({'kind': 'stale-pr-candidate', 'repo': repo, 'pr': pr['number'], 'draft': pr['isDraft']})
                checks = pr.get('statusCheckRollup') or []
                red = [c.get('name', c.get('context')) for c in checks
                       if (c.get('conclusion') or c.get('state')) in {'FAILURE', 'ERROR', 'TIMED_OUT', 'ACTION_REQUIRED', 'CANCELLED'}]
                if red: findings.append({'kind': 'pr-ci-failed', 'repo': repo, 'pr': pr['number'], 'sha': pr['headRefOid'], 'checks': red})
            runs = api(f'repos/{repo}/actions/runs?branch={quote(branch, safe="")}&per_page=100')['workflow_runs']
            latest = {}
            for run in runs: latest.setdefault(run['workflow_id'], run)
            for run in latest.values():
                if run.get('conclusion') in {'failure', 'timed_out', 'action_required', 'cancelled'}:
                    findings.append({'kind': 'default-branch-ci-failed', 'repo': repo, 'run': run['id'], 'url': run['html_url']})
            if len(runs) == 100: gaps.append(repo + ': only latest 100 default-branch runs checked')
        except Exception as exc:
            failed = True
            gaps.append(repo + ': GitHub PR/CI read failed (' + type(exc).__name__ + ')')
        if entry.get('catalog'):
            try:
                # Resolve independently even if the PR/CI query failed.
                branch = api(f'repos/{repo}')['default_branch']
                manifest = content(repo, entry['catalog'], branch)
                findings.extend(catalog_findings(repo, branch, manifest))
                counts['catalogs'] += 1
            except Exception as exc:
                failed = True
                gaps.append(repo + ': catalog read/resolve failed (' + type(exc).__name__ + ')')
    return {'counts': counts, 'findings': findings, 'coverage_gaps': gaps, 'failed': failed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--github-config', type=Path, default=Path(__file__).with_name('stale_sweep_repos.json'))
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--days', type=int, default=14)
    parser.add_argument('--post', action='store_true')
    args = parser.parse_args()
    if args.days < 1: parser.error('--days must be positive')
    try:
        data = json.loads(args.snapshot.read_text()) if args.snapshot else snapshot(os.environ.get('STALE_SWEEP_READ_TOKEN'))
        report = census(data, days=args.days)
        github = github_census(json.loads(args.github_config.read_text()), days=args.days)
        report['counts'].update(github['counts'])
        report['findings'].extend(github['findings'])
        report['coverage_gaps'].extend(github['coverage_gaps'])
        report['github_read_failed'] = github['failed']
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
        if args.post: post_report(report, os.environ.get('OPS_SERVER_TOKEN'))
        print('Stale sweep finished; report saved; worklog ' + ('recorded' if args.post else 'not requested'))
        return 1 if github['failed'] else 0
    except Exception as exc:
        print('Stale sweep failed: ' + type(exc).__name__, file=sys.stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
