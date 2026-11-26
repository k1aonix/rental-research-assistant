import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from rental.common import DomainError,decode,encode,digest
from rental.config import Settings
from rental.station_table import StationTableRepository,STRATEGY,convert_csv
from rental.providers import MapGateway


def fixture():
    settings=Settings()
    return {'version':'isolated-v1','target_id':settings.target_poi,'target_snapshot':settings.fixed_config(),
            'time_basis':'人工测试资料，未指定日期','transport_mode':'地铁及步行','prepared_at':'2026-11-21',
            'source_note':'隔离测试，不是实际业务站点','coverage_limit_sec':3600,
            'stations':[{'station_id':name,'station_name':name,'poi_id':name,'location':f'114.{i+1},22.6','coordinate_system':'GCJ-02',
                         'reference_duration_min':minutes,'display_order':i,'enabled':True,'aliases':[name]}
                        for i,(name,minutes) in enumerate([('later',40),('equal-b',30),('equal-a',30),('over',60)])]}

def frozen(table):
    value=StationTableRepository.validate(table,Settings().fixed_config())
    return {'strategy':STRATEGY,'table_version':value['version'],'table_hash':value['table_hash'],'table':value}

class StationTableTest(unittest.TestCase):
    def setUp(self):self.target=Settings().fixed_config()
    def test_duration_boundary_and_stable_original_order(self):
        result=StationTableRepository.select(self.target,1800,frozen(fixture()))
        self.assertEqual([s['station_id'] for s in result['stations']],['equal-b','equal-a'])
        self.assertEqual(result['excluded'][0]['reason'],'over_reference_limit')
    def test_fractional_minutes_convert_exact_seconds(self):
        table=fixture();table['stations'][0]['reference_duration_min']='19.55'
        self.assertEqual(frozen(table)['table']['stations'][0]['reference_duration_sec'],1173)
        table['stations'][0]['reference_duration_min']='29.917'
        with self.assertRaises(DomainError):frozen(table)
    def test_invalid_missing_nan_negative_bool_and_range_time(self):
        for value in (None,'',float('nan'),float('inf'),-1,True,'约30～40'):
            table=fixture();table['stations'][0]['reference_duration_min']=value
            with self.subTest(value=value),self.assertRaises(DomainError) as error:frozen(table)
            self.assertEqual(error.exception.code,'STATION_TABLE_INVALID');self.assertEqual(error.exception.details['row'],1)
    def test_zero_requires_explicit_confirmation(self):
        table=fixture();table['stations'][0]['reference_duration_min']=0
        with self.assertRaises(DomainError):frozen(table)
        table['stations'][0]['zero_confirmed']=True
        self.assertEqual(StationTableRepository.select(self.target,0,frozen(table))['stations'][0]['station_id'],'later')
    def test_duplicate_id_poi_and_physical_location_rejected(self):
        for field in ('station_id','poi_id','location'):
            table=fixture();table['stations'][1][field]=table['stations'][0][field]
            with self.subTest(field=field),self.assertRaises(DomainError):frozen(table)
    def test_missing_identity_unknown_coordinate_and_bad_header(self):
        for field,value in (('station_name',''),('poi_id',''),('coordinate_system','unknown'),('enabled','true'),('display_order',False),('aliases','x')):
            table=fixture();table['stations'][0][field]=value
            if field=='poi_id':table['stations'][0].pop('location')
            with self.subTest(field=field),self.assertRaises(DomainError):frozen(table)
        table=fixture();table['target_snapshot']=None
        with self.assertRaises(DomainError):frozen(table)
    def test_target_mismatch_id_and_coordinates(self):
        for field,value in (('target_id','other'),('target_snapshot',{'target_poi':self.target['target_poi'],'location':'114,22'})):
            table=fixture();table[field]=value
            with self.assertRaises(DomainError) as error:frozen(table)
            self.assertEqual(error.exception.code,'STATION_TABLE_TARGET_MISMATCH')
    def test_disabled_and_real_empty_result(self):
        table=fixture();table['stations'][0]['enabled']=False
        result=StationTableRepository.select(self.target,1,frozen(table))
        self.assertEqual(result['outcome'],'no_station_within_reference_limit');self.assertEqual(result['stations'],[])
    def test_table_coverage_does_not_clamp_profile_limit(self):
        result=StationTableRepository.select(self.target,5400,frozen(fixture()))
        self.assertEqual(result['commute_limit_sec'],5400);self.assertTrue(result['table_coverage_limited']);self.assertEqual(len(result['stations']),4)
    def test_missing_or_invalid_file_never_returns_empty(self):
        for path in ('',str(Path(tempfile.gettempdir())/'rental-no-such-station-table.json')):
            repository=StationTableRepository(path,self.target)
            with self.assertRaises(DomainError) as error:repository.select(self.target,2400,repository.freeze())
            self.assertEqual(error.exception.code,'STATION_TABLE_REQUIRED')
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'bad.json';p.write_text('broken')
            self.assertEqual(StationTableRepository(p,self.target).error.code,'STATION_TABLE_INVALID')
    def test_snapshot_tamper_and_legacy_policy_block(self):
        value=frozen(fixture());value['table']['stations'][0]['reference_duration_sec']=1;value['table']['stations'][0].pop('reference_duration_min')
        with self.assertRaises(DomainError) as error:StationTableRepository.select(self.target,2400,value)
        self.assertEqual(error.exception.code,'STATION_TABLE_SNAPSHOT_INVALID')
        with self.assertRaises(DomainError) as error:StationTableRepository.select(self.target,2400,{'strategy':'amap_isochrone_stations_v1'})
        self.assertEqual(error.exception.code,'LEGACY_DISCOVERY_POLICY')
    def test_map_compatibility_entry_uses_no_network_or_walk_budget(self):
        maps=MapGateway(Settings(amap_key=''))
        with patch('urllib.request.urlopen',side_effect=AssertionError('No network')),patch.object(maps,'_client',side_effect=AssertionError('No map client')):
            a=maps.station_plan(self.target,2400,{'station_selection':frozen(fixture()),'walk_limit_sec':0})
            b=maps.station_plan(self.target,2400,{'station_selection':frozen(fixture()),'walk_limit_sec':10000})
        self.assertEqual(a,b)
    def test_real_csv_exact_seconds_count_and_coverage(self):
        repository=StationTableRepository(Settings().station_table,self.target)
        self.assertIsNone(repository.error);self.assertEqual(len(repository.table['stations']),52)
        for minutes,count in ((20,1),(30,2),(40,13),(60,52),(90,52)):
            self.assertEqual(len(repository.select(self.target,minutes*60,repository.freeze())['stations']),count)
        self.assertTrue(any(s['reference_duration_sec']==1795 for s in repository.table['stations']))
        raw = Path(Settings().station_table).parent / '60分钟以内地铁站.csv'
        if not raw.is_file():
            self.skipTest('Private station CSV is not available')
        before = raw.read_bytes()
        with tempfile.TemporaryDirectory() as folder:
            converted=convert_csv(raw.parent,Path(folder)/'normalized.json',self.target)
            self.assertEqual(len(converted['stations']),52);self.assertEqual(raw.read_bytes(),before)
    def test_snapshot_survives_file_change_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'table.json';p.write_text(json.dumps(fixture()),encoding='utf-8')
            before=StationTableRepository(p,self.target).freeze();table=fixture();table['version']='v2';table['stations'][0]['enabled']=False;p.write_text(json.dumps(table),encoding='utf-8')
            after=StationTableRepository(p,self.target).freeze()
            self.assertNotEqual(before['table_hash'],after['table_hash'])
            self.assertEqual(len(StationTableRepository.select(self.target,2400,before)['stations']),3)
            self.assertEqual(len(StationTableRepository.select(self.target,2400,after)['stations']),2)
    def test_capabilities_configured_without_any_amap_key_or_enterprise_permission(self):
        maps=MapGateway(Settings());cap=maps.capabilities()['station_discovery']
        self.assertEqual(cap['strategy'],STRATEGY);self.assertEqual(cap['status'],'configured');self.assertFalse(cap['requires_product_permission'])

from . import test_system as base
class StationTableFlowTest(unittest.TestCase):
    setUp=base.SystemTest.setUp
    tearDown=base.SystemTest.tearDown
    write=base.SystemTest.write
    prepare=base.SystemTest.prepare
    refresh=base.SystemTest.refresh
    lock_profile=base.SystemTest.lock_profile
    command=base.SystemTest.command
    drain=base.SystemTest.drain
    def force_round_two(self,stations):
        task=self.write('/tasks/search',self.command())['data'];row=self.db.one('SELECT * FROM tasks WHERE task_id=?',(task['task_id'],))
        progress=decode(row['progress']);progress['round']=2;progress.pop('station_selection',None);progress.pop('station_selection_key',None)
        self.db.connection.execute('UPDATE tasks SET progress=? WHERE task_id=?',(encode(progress),task['task_id']))
        self.maps.station_plan=Mock(return_value={'quality':'success','strategy':STRATEGY,'stations':stations,'table_version':'test-v1'})
        return task
    def test_new_task_freezes_full_table_and_capabilities_endpoint(self):
        self.prepare();task=self.write('/tasks/search',self.command())['data']
        self.assertEqual(task['config_snapshot']['station_selection_strategy'],STRATEGY)
        self.assertNotIn('table',task['config_snapshot']['station_selection'])
        stored=decode(self.db.one('SELECT progress FROM tasks WHERE task_id=?',(task['task_id'],))['progress'])
        self.assertEqual(len(stored['config_snapshot']['station_selection']['table']['stations']),52)
        self.assertEqual(self.client.get('/api/v1/capabilities').status_code,200)
    def test_partial_coverage_completes_available_sources_with_explicit_gap(self):
        self.prepare();self.db.connection.execute("INSERT INTO searches(import_id,query_text,region,station_poi,source_order,quality) VALUES(1,'station a','near_metro','a',2,'empty_confirmed')")
        task=self.force_round_two([{'poi_id':'a'},{'poi_id':'b'}]);done=self.drain(task['task_id'])
        self.assertEqual(done['state'],'completed',done);self.assertEqual(done['result']['outcome'],'available_snapshots_exhausted_with_coverage_missing')
        self.assertEqual(done['station_selection']['snapshot_coverage']['missing_station_ids'],['b'])
    def test_all_missing_fails_and_retry_reuses_selection(self):
        self.prepare();task=self.force_round_two([{'poi_id':'a'}]);failed=self.drain(task['task_id'])
        self.assertEqual(failed['error']['code'],'STATION_SNAPSHOT_NOT_COLLECTED')
        self.write('/tasks/'+task['task_id']+'/retry',self.command(failed['version']),expected=409);again=self.client.get('/api/v1/tasks/'+task['task_id']).json()['data']
        self.assertEqual(again['error']['code'],'STATION_SNAPSHOT_NOT_COLLECTED');self.assertEqual(self.maps.station_plan.call_count,1)
    def test_incomplete_positions_are_a_gap_and_not_an_empty_snapshot(self):
        self.prepare();self.db.connection.execute("INSERT INTO searches(import_id,query_text,region,station_poi,source_order,quality) VALUES(1,'bad','near_metro','a',2,'success')")
        task=self.force_round_two([{'poi_id':'a'}]);failed=self.drain(task['task_id'])
        self.assertEqual(failed['error']['code'],'STATION_SNAPSHOT_NOT_COLLECTED')
        self.assertEqual(failed['station_selection']['snapshot_coverage']['snapshot_issues'][0]['state'],'snapshot_incomplete')
    def test_same_snapshot_cannot_bind_two_stations_and_source_version_must_match(self):
        self.prepare();self.db.connection.execute("INSERT INTO searches(import_id,query_text,region,station_poi,source_order,quality) VALUES(1,'q','near_metro',NULL,2,'empty_confirmed')")
        batch=self.db.one('SELECT * FROM imports WHERE import_id=1');manifest=decode(batch['source_manifest']);row=self.db.one("SELECT search_key FROM searches WHERE query_text='q'")
        manifest['searches'][str(row['search_key'])]={'search_id':'s','returned_count':0};self.db.connection.execute('UPDATE imports SET source_manifest=? WHERE import_id=1',(encode(manifest),))
        ref={'verified':True,'source_version':batch['version'],'search_id':'s','keyword':'q'}
        stations=[{'station_id':'a','snapshot_refs':[ref]},{'station_id':'b','snapshot_refs':[ref]}]
        with self.assertRaises(DomainError) as error:self.app.state.services.source.station_coverage(1,stations)
        self.assertEqual(error.exception.code,'SNAPSHOT_BINDING_AMBIGUOUS')
        stations[0]['snapshot_refs'][0]={**ref,'source_version':'other'}
        self.assertEqual(self.app.state.services.source.station_coverage(1,stations[:1])['covered_count'],0)
    def test_missing_table_blocks_before_task_and_model_calls(self):
        self.prepare();self.app.state.services.station_tables=StationTableRepository('',Settings().fixed_config())
        self.maps.station_plan=lambda target,limit,context:StationTableRepository.select(target,limit,context['station_selection'])
        failed=self.write('/tasks/search',self.command(),expected=409)
        self.assertEqual(failed['error']['code'],'STATION_TABLE_REQUIRED')
        self.assertEqual(self.db.one('SELECT COUNT(*) AS n FROM tasks')['n'],0);self.assertEqual(sum(self.model.calls.values()),0)
    def test_old_task_cannot_silently_switch_or_call_maps(self):
        self.prepare();task=self.force_round_two([])
        row=self.db.one('SELECT * FROM tasks WHERE task_id=?',(task['task_id'],));progress=decode(row['progress']);progress['config_snapshot'].pop('station_selection_strategy')
        self.db.connection.execute('UPDATE tasks SET progress=? WHERE task_id=?',(encode(progress),task['task_id']))
        done=self.drain(task['task_id']);self.assertEqual(done['error']['code'],'LEGACY_DISCOVERY_POLICY');self.maps.station_plan.assert_not_called()
        self.write('/tasks/'+task['task_id']+'/retry',self.command(done['version']),expected=409)

    def test_preflight_blocks_missing_before_first_round_and_requires_partial_consent(self):
        self.prepare();self.maps.station_plan=Mock(return_value={'quality':'success','strategy':STRATEGY,'stations':[{'poi_id':'a','name':'甲站'},{'poi_id':'b','name':'乙站'}]})
        p=self.client.get('/api/v1/search-preflight').json()['data']
        self.assertFalse(p['can_start']);self.assertEqual(p['coverage']['missing_station_ids'],['a','b'])
        self.write('/tasks/search',self.command(),expected=409)
        self.assertEqual(self.db.one('SELECT COUNT(*) AS n FROM tasks')['n'],0);self.assertEqual(sum(self.model.calls.values()),0)
        self.db.connection.execute("INSERT INTO searches(import_id,query_text,region,station_poi,source_order,quality) VALUES(1,'甲站','near_metro','a',2,'empty_confirmed')")
        p=self.client.get('/api/v1/search-preflight').json()['data'];self.assertTrue(p['can_start']);self.assertTrue(p['requires_partial_confirmation'])
        failed=self.write('/tasks/search',self.command(),expected=409);self.assertEqual(failed['error']['code'],'SEARCH_COVERAGE_CONFIRMATION_REQUIRED')
        failed=self.write('/tasks/search',{**self.command(),'preflight_token':'stale','allow_partial':True},expected=409);self.assertEqual(failed['error']['code'],'SEARCH_PREFLIGHT_CHANGED')
        task=self.write('/tasks/search',{**self.command(),'preflight_token':p['preflight_token'],'allow_partial':True})['data']
        self.assertEqual(task['search_preflight']['coverage']['covered_count'],1);self.assertEqual(task['search_preflight']['coverage']['missing_station_ids'],['b'])
        self.assertEqual(sum(self.model.calls.values()),0)

    def test_author_revision_inherits_verified_identical_snapshot_but_rejects_changed_rows(self):
        from rental.source_repair import repair_authors
        from rental.common import now
        self.prepare(author_status='not_attempted');batch=self.db.one('SELECT * FROM imports WHERE import_id=1');manifest=decode(batch['source_manifest'])
        key=self.db.connection.execute("INSERT INTO searches(import_id,query_text,region,source_order,quality) VALUES(1,'q','near_metro',2,'success')").lastrowid
        self.db.connection.execute('INSERT INTO search_results(search_key,position,raw_note_id,raw_title,quality) VALUES(?,1,?,?,?)',(key,base.nid(1),'原始标题','success'))
        manifest['searches'][str(key)]={'search_id':'bound-q','keyword':'q','returned_count':1};self.db.connection.execute('UPDATE imports SET source_manifest=? WHERE import_id=1',(encode(manifest),))
        station={'station_id':'a','snapshot_refs':[{'verified':True,'source_version':batch['version'],'search_id':'bound-q','keyword':'q'}]}
        report={'source':'live_author_profile','fetched_at':now(),'data':{'author_id':base.AUTHOR,'quality':'success','history_titles':[{'note_id':base.nid(100),'title':'历史标题','original_url':'https://www.xiaohongshu.com/explore/'+base.nid(100)}]}}
        revised=repair_authors(self.db,self.command()['profile_id'],[report],'author-only',apply=True)['to_import_id']
        coverage=self.app.state.services.source.station_coverage(revised,[station]);self.assertEqual(coverage['covered_count'],1)
        self.assertEqual(len(self.app.state.services.source.search(revised,'near_metro',[station])),1)
        child=coverage['bindings'][0]['search_keys'][0]
        self.db.connection.execute("UPDATE search_results SET raw_title='已变化' WHERE search_key=?",(child,))
        coverage=self.app.state.services.source.station_coverage(revised,[station]);self.assertEqual(coverage['covered_count'],0)
        self.assertEqual(coverage['snapshot_issues'][0]['state'],'binding_revision_mismatch')

    def test_same_task_retry_after_verified_binding_repair_preserves_first_round(self):
        self.prepare();task=self.force_round_two([{'poi_id':'a'}]);failed=self.drain(task['task_id']);before=failed['counters']['near_target'].copy()
        self.db.connection.execute("INSERT INTO searches(import_id,query_text,region,station_poi,source_order,quality) VALUES(1,'a','near_metro','a',2,'empty_confirmed')")
        view=self.client.get('/api/v1/tasks/'+task['task_id']).json()['data'];self.assertTrue(view['recovery']['can_retry'])
        self.assertNotIn('新建任务',view['error']['message'])
        self.write('/tasks/'+task['task_id']+'/retry',self.command(view['version']));done=self.drain(task['task_id'])
        self.assertEqual(done['state'],'completed');self.assertEqual(done['task_id'],task['task_id']);self.assertEqual(done['counters']['near_target'],before)
        self.assertEqual(self.maps.station_plan.call_count,1);self.assertEqual(sum(self.model.calls.values()),0)

    def test_historical_table_date_and_current_property_planning_date_are_separate(self):
        self.prepare();self.settings.reference_date='2026-11-22';self.settings.calendar_policy='actual_date'
        self.db.connection.execute('UPDATE profiles SET commute_limit_sec=5400')
        self.maps.station_plan=lambda target,limit,context:StationTableRepository.select(target,limit,context['station_selection'])
        p=self.client.get('/api/v1/search-preflight').json()['data']
        self.assertEqual(p['planning_context']['reference_date'],'2026-11-22');self.assertIn('2026-11-21',p['table_time_basis'])
        self.assertTrue(p['table_coverage_limited']);self.assertEqual(p['table_coverage_limit_sec'],3600)
        self.assertEqual(p['error']['code'],'STATION_SNAPSHOT_NOT_COLLECTED')
