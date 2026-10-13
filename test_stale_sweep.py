import importlib.util
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('sweep',Path(__file__).parent/'scripts/stale_sweep.py')
sweep=importlib.util.module_from_spec(spec);spec.loader.exec_module(sweep)
NOW=datetime(2026,10,12,tzinfo=timezone.utc)

class SweepTests(unittest.TestCase):
    def test_age_is_candidate_and_pause_is_respected(self):
        rows=[{'id':s,'state':s,'updated_at':'2026-09-01T00:00:00Z'} for s in ('ready','paused','deferred','done')]
        result=sweep.census({'list_work_items':rows},now=NOW)
        self.assertEqual([f['id'] for f in result['findings']],['ready'])
        self.assertEqual(rows[0]['state'],'ready')
        self.assertEqual(result['coverage_gaps'],[])

    def test_snapshot_expands_capped_projects_and_reports_remaining_gap(self):
        def rpc(name,args,token):
            if name=='list_projects':return [{'code':'nova'}]
            if name=='list_modules':return []
            return [{'id':str(i)} for i in (range(500,510) if 'project' in args else range(500))]
        with patch.object(sweep,'rpc',side_effect=rpc):result=sweep.snapshot('fake')
        self.assertEqual(len(result['list_work_items']),510)
        self.assertTrue(result['coverage_gaps'])

    def test_worklog_requires_persisted_id_and_bounds_details(self):
        report={'counts':{},'coverage_gaps':[],'findings':[{'kind':'candidate'}]*30}
        with patch.object(sweep,'rpc',return_value={'id':'bad'}) as rpc:
            with self.assertRaises(ValueError):sweep.post_report(report,'fake')
            import json
            detail=json.loads(rpc.call_args.args[1]['detailed'])
            self.assertEqual(detail['omitted_findings'],5)
            self.assertEqual(len(detail['findings']),25)

    def test_hosted_never_checks_local_files(self):
        with patch.object(sweep.Path,'is_dir',side_effect=AssertionError('local access')):
            result=sweep.census({'list_projects':[{'id':'p','folder_path':'AgentSecretBase/project','status':'paused'}]},now=NOW)
        self.assertEqual(result['findings'],[])
        self.assertEqual(result['coverage_gaps'],[])

    def test_github_reports_pr_and_latest_workflow_failure(self):
        def api(path):
            if '/actions/runs?' in path:
                return {'workflow_runs':[
                    {'workflow_id':1,'id':10,'conclusion':'success','html_url':'ok'},
                    {'workflow_id':1,'id':9,'conclusion':'failure','html_url':'old'},
                    {'workflow_id':2,'id':8,'conclusion':'failure','html_url':'bad'}]}
            return {'default_branch':'main'}
        prs=[{'number':3,'updatedAt':'2026-09-01T00:00:00Z','isDraft':False,'headRefOid':'abc','statusCheckRollup':[{'name':'build','conclusion':'FAILURE'}]}]
        with patch.object(sweep,'api',side_effect=api), patch.object(sweep,'gh_json',return_value=prs):
            result=sweep.github_census({'repositories':[{'repo':'Nova-Caelum/example'}]},now=NOW)
        self.assertFalse(result['failed'])
        self.assertEqual([f['kind'] for f in result['findings']],['stale-pr-candidate','pr-ci-failed','default-branch-ci-failed'])
        self.assertEqual(result['findings'][-1]['run'],8)

    def test_github_failure_is_not_reported_as_empty_success(self):
        with patch.object(sweep,'api',side_effect=ValueError('SECRET')):
            result=sweep.github_census({'repositories':[{'repo':'Nova-Caelum/example'}]})
        self.assertTrue(result['failed'])
        self.assertNotIn('SECRET',str(result))
        self.assertIn('ValueError',result['coverage_gaps'][0])

    def test_catalog_pins_resolve_and_changes_only_flag_candidates(self):
        pin='a'*40
        manifest={'plugins':[{'name':'base','source':{'source':'git-subdir','url':'https://github.com/Nova-Caelum/example.git','path':'plugins/base','sha':pin}}]}
        def api(path):
            if '/compare/' in path:return {'status':'ahead','files':[{'filename':'plugins/base/README.md'}]}
            return {'default_branch':'main'}
        with patch.object(sweep,'content',return_value={'name':'base'}) as content,patch.object(sweep,'api',side_effect=api):
            findings=sweep.catalog_findings('Nova-Caelum/example','main',manifest)
        self.assertEqual(findings[0]['kind'],'catalog-pin-review-candidate')
        content.assert_called_once_with('Nova-Caelum/example','plugins/base/.claude-plugin/plugin.json',pin)
        self.assertEqual(manifest['plugins'][0]['source']['sha'],pin)

    def test_catalog_relative_source_has_no_invented_pin(self):
        with patch.object(sweep,'content',return_value={'name':'base'}) as content:
            self.assertEqual(sweep.catalog_findings('Nova-Caelum/example','main',{'plugins':[{'name':'base','source':'./'}]}),[])
        content.assert_called_once_with('Nova-Caelum/example','.claude-plugin/plugin.json','main')

if __name__=='__main__':unittest.main()
