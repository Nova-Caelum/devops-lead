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
        self.assertTrue(result['coverage_gaps'])

    def test_folder_paths_are_confined_and_absence_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'exists').mkdir()
            self.assertEqual(sweep.pointer('exists',root),'exists')
            self.assertEqual(sweep.pointer('../escape',root),'unverified: outside vault')
            self.assertEqual(sweep.pointer('/elsewhere',root),'unverified: outside vault')
            self.assertEqual(sweep.pointer('missing',root),'broken')
            self.assertEqual(sweep.pointer('exists',None),'unverified: no local vault mounted')

    def test_inventory_repairs_only_generated_file_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);catalog=root/'AgentSecretBase/catalog';catalog.mkdir(parents=True)
            index=catalog/'INDEX.md';index.write_text('authored index')
            (catalog/'outside').symlink_to('/etc/hosts')
            first=sweep.census({},root,now=NOW,write_inventory=True)
            second=sweep.census({},root,now=NOW,write_inventory=True)
            self.assertEqual(index.read_text(),'authored index')
            self.assertIn('catalog-inventory-regenerated',[f['kind'] for f in first['findings']])
            self.assertNotIn('catalog-inventory-regenerated',[f['kind'] for f in second['findings']])
            self.assertEqual(second['counts']['catalog_files'],1)

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

    def test_access_gate_does_not_claim_dashboard_health(self):
        import urllib.error
        with patch.object(sweep.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=urllib.error.HTTPError('https://task.novacaelum.com',302,'gate',{},None)
            self.assertEqual(sweep.dashboard_probe(),{'http_status':302,'ui_freshness':'unverified'})

if __name__=='__main__':unittest.main()
