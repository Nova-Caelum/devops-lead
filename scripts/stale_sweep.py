#!/usr/bin/env python3
"""Deterministic stale candidates; never close work or rewrite release content."""
import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request
import uuid

OPS = 'https://nova-caelum-ops-production.up.railway.app/mcp'
TERMINAL = {'done', 'completed', 'closed', 'archived', 'descoped', 'killed'}
INVENTORY = '.stale-sweep-inventory.json'


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


def pointer(value, vault):
    if not value or not str(value).strip():
        return 'missing'
    if vault is None:
        return 'unverified: no local vault mounted'
    raw = str(value).strip()
    # Only resolve paths inside the explicitly supplied vault. Never probe arbitrary hosts.
    if any(part in raw for part in (' + ', ' (', '\n')):
        return 'unverified: composite pointer'
    marker = 'NovaCaelum_Obs/'
    if marker in raw:
        raw = raw.split(marker, 1)[1]
    elif raw.startswith(('~', '/')):
        return 'unverified: outside vault'
    path = (vault / raw).resolve()
    if not path.is_relative_to(vault.resolve()):
        return 'unverified: outside vault'
    return 'exists' if path.is_dir() else 'broken'


def catalog_inventory(catalog):
    files = []
    for path in sorted(catalog.rglob('*')):
        if path.is_symlink() or not path.is_file() or path.name == INVENTORY:
            continue
        if any(p.startswith('.') or p == 'node_modules' for p in path.relative_to(catalog).parts):
            continue
        files.append({'path': str(path.relative_to(catalog)), 'bytes': path.stat().st_size,
                      'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return {'schema_version': 1, 'files': files}


def census(data, vault=None, now=None, days=14, write_inventory=False):
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
                check = pointer(row.get('folder_path'), vault)
                if check != 'exists':
                    findings.append({'kind': 'folder-' + check, 'surface': kind, 'id': ident,
                                     'pointer': row.get('folder_path')})
                if kind == 'list_modules' and not row.get('code_repo'):
                    findings.append({'kind': 'code-repo-missing', 'surface': kind, 'id': ident})
    if vault is None:
        gaps += ['catalog and local loop states unavailable on hosted runner',
                 'folder existence needs a mounted vault; missing pointers remain detectable']
    else:
        workspace = vault / 'AgentSecretBase/workspace'
        loops = sorted(workspace.rglob('loop.state.json')) if workspace.is_dir() else []
        counts['local_loops'] = len(loops)
        if not workspace.is_dir(): gaps.append('workspace missing from supplied vault')
        for path in loops:
            if path.is_symlink(): continue
            try: row = json.loads(path.read_text())
            except (OSError, ValueError):
                findings.append({'kind': 'invalid-loop-json', 'path': str(path.relative_to(vault))}); continue
            if row.get('status') in TERMINAL or row.get('status') == 'paused': continue
            # This is evidence of last recorded activity, not proof nobody is working.
            dates = [timestamp(row.get('updated_at'))]
            for entry in row.get('trail', []):
                dates.extend(timestamp(entry.get(k)) for k in ('entered_at', 'exited_at'))
            latest = max((d for d in dates if d is not None), default=None)
            if latest is None or latest < cutoff:
                findings.append({'kind': 'loop-review-candidate', 'path': str(path.relative_to(vault)),
                                 'status': row.get('status'), 'last_recorded_activity': latest.isoformat() if latest else None})
        catalog = vault / 'AgentSecretBase/catalog'
        if catalog.is_dir():
            if not catalog.resolve().is_relative_to(vault.resolve()):
                raise ValueError('catalog must be inside supplied vault')
            inventory = catalog_inventory(catalog)
            target = catalog / INVENTORY
            serialized = json.dumps(inventory, indent=2, ensure_ascii=False) + '\n'
            changed = not target.exists() or target.read_text() != serialized
            counts['catalog_files'] = len(inventory['files'])
            if changed:
                findings.append({'kind': 'catalog-inventory-regenerated' if write_inventory else 'catalog-inventory-stale'})
                if write_inventory:
                    # Only this generated file is repaired. Human index/archive content is untouched.
                    if target.is_symlink(): raise ValueError('inventory must not be a symlink')
                    target.write_text(serialized)
        else: gaps.append('catalog missing from supplied vault')
    gaps.append('dashboard HTTP probe cannot establish authenticated UI/data freshness')
    return {'observed_at': now.isoformat(), 'candidate_age_days': days, 'counts': counts,
            'coverage_gaps': gaps, 'findings': findings}


def dashboard_probe():
    # Fixed public URL, no ops credential; redirects indicate an access gate, not UI health.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    try:
        with urllib.request.build_opener(NoRedirect).open('https://task.novacaelum.com', timeout=15) as resp:
            return {'http_status': resp.status, 'ui_freshness': 'unverified'}
    except urllib.error.HTTPError as exc:
        return {'http_status': exc.code, 'ui_freshness': 'unverified'}
    except Exception as exc:
        return {'error': type(exc).__name__, 'ui_freshness': 'unverified'}


def post_report(report, token):
    kinds = Counter(f['kind'] for f in report['findings'])
    summary = 'daily stale sweep: ' + ', '.join(f'{k}={v}' for k,v in report['counts'].items())
    summary += f"; candidates={len(report['findings'])}; coverage gaps={len(report['coverage_gaps'])}"
    # Bound transmission; full report stays local. Explicitly indicate omitted records.
    detail = {**report, 'finding_counts': dict(kinds), 'findings': report['findings'][:25],
              'omitted_findings': max(0, len(report['findings']) - 25)}
    result = rpc('append_worklog', {'author': 'devops-lead', 'project': 'governance-evolution-sprint',
        'summary': summary[:500], 'detailed': json.dumps(detail, ensure_ascii=False),
        'tags': ['daily-stale-sweep', 'row-22', 'review-candidates']}, token)
    uuid.UUID(result['id']) # A success response without a persisted row ID is not sufficient.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--vault-root', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--days', type=int, default=14)
    parser.add_argument('--write-catalog-inventory', action='store_true')
    parser.add_argument('--post', action='store_true')
    args = parser.parse_args()
    if args.days < 1: parser.error('--days must be positive')
    if args.write_catalog_inventory and args.vault_root is None: parser.error('inventory writes require --vault-root')
    try:
        data = json.loads(args.snapshot.read_text()) if args.snapshot else snapshot(os.environ.get('STALE_SWEEP_READ_TOKEN'))
        report = census(data, args.vault_root, days=args.days, write_inventory=args.write_catalog_inventory)
        report['dashboard'] = dashboard_probe()
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
        if args.post: post_report(report, os.environ.get('OPS_SERVER_TOKEN'))
        print('Stale sweep finished; report saved; worklog ' + ('recorded' if args.post else 'not requested'))
        return 0
    except Exception as exc:
        # Public CI logs must not contain internal row names, API bodies or credentials.
        print('Stale sweep failed: ' + type(exc).__name__, file=sys.stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
