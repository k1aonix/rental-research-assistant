import copy,unittest
from . import test_system as base
from rental.source import SourceImporter
from rental.common import decode

class SourceRepairTest(unittest.TestCase):
    setUp=base.SystemTest.setUp
    tearDown=base.SystemTest.tearDown
    write=base.SystemTest.write
    prepare=base.SystemTest.prepare
    refresh=base.SystemTest.refresh
    lock_profile=base.SystemTest.lock_profile
    command=base.SystemTest.command
    drain=base.SystemTest.drain

    def test_phase_alias_normalization_does_not_merge_distinct_phases(self):
        from rental.providers import canonical_place_name
        self.assertEqual(canonical_place_name('颐安都会中央三期'),canonical_place_name('颐安·都会中央3期'))
        self.assertNotEqual(canonical_place_name('颐安都会中央一期'),canonical_place_name('颐安·都会中央3期'))

    def test_author_source_failure_degrades_without_brief_or_stale_task_error(self):
        self.prepare(author_status='failed')
        task=self.write('/tasks/search',self.command())['data'];done=self.drain(task['task_id'])
        self.assertEqual(done['state'],'completed',done)
        self.assertIsNone(done['error'])
        self.assertFalse(done['result']['brief_ids'])
        self.assertEqual(done['counters']['near_target']['candidate_added_count'],1)

    def broken(self):
        data,manifest=self.prepare()
        row=self.db.one('SELECT * FROM imports');broken=decode(row['source_manifest'])
        for value in broken['searches'].values():value.pop('discovery_scope',None)
        self.db.connection.execute('UPDATE imports SET source_manifest=?',(base.json.dumps(broken),))
        task=self.write('/tasks/search',self.command())['data'];task=self.drain(task['task_id'])
        self.assertEqual(task['state'],'failed');self.assertEqual(task['error']['code'],'SOURCE_COVERAGE_MISSING');self.assertEqual(self.model.calls,{})
        return data,manifest,task

    def test_valid_repaired_batch_retry_continues_same_task(self):
        data,manifest,failed=self.broken();new=SourceImporter(self.db).import_data(data,manifest,'verified-source-v2')
        retried=self.write('/tasks/'+failed['task_id']+'/retry',self.command(failed['version']))['data']
        self.assertEqual(retried['task_id'],failed['task_id']);self.assertEqual(retried['config_snapshot']['import_id'],new)
        done=self.drain(failed['task_id']);self.assertEqual(done['state'],'completed',done)
        self.assertGreater(done['counters']['near_target']['attempt_count'],0)

    def test_flag_alone_outside_scope_target_mismatch_and_lost_positions_stay_blocked(self):
        data,manifest,failed=self.broken()
        for variant in ['flag_only','outside','wrong_target','lost_positions']:
            revised=copy.deepcopy(manifest);raw=copy.deepcopy(data);meta=revised['search_mappings']['near-school']
            if variant=='flag_only':meta.pop('discovery_scope')
            if variant=='outside':meta['discovery_scope']['anchor']['walk_sec']=1201
            if variant=='wrong_target':meta['discovery_scope']['target_poi']='another-place'
            if variant=='lost_positions':raw['search_runs'][0]['returned_count']=2
            SourceImporter(self.db).import_data(raw,revised,'invalid-'+variant)
            self.write('/tasks/'+failed['task_id']+'/retry',self.command(failed['version']),expected=503)
            current=self.client.get('/api/v1/tasks/'+failed['task_id']).json()['data']
            self.assertEqual(current['state'],'failed');self.assertEqual(current['config_snapshot']['import_id'],1)
            self.assertEqual(self.model.calls,{})
