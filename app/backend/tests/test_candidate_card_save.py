import unittest
from . import test_system as base


class CandidateCardSaveTest(unittest.TestCase):
    def setUp(self):
        self.fx = base.SystemTest()
        self.fx.setUp()

    def tearDown(self):
        self.fx.tearDown()

    def candidate(self, notes=1):
        self.fx.prepare(notes=notes)
        task = self.fx.manual()
        task = self.fx.drain(task['task_id'])
        return self.fx.record(task)

    def payload(self, record):
        return {**self.fx.command(record['version']), 'group': 'all', 'rent_cent': 355500,
                'monthly_cent': 375500, 'initial_cent': 1066500, 'commute_sec': 1800, 'remark': '一起保存的备注'}

    def test_one_save_updates_all_fields_once_without_changing_report(self):
        record = self.candidate()
        before_report = self.fx.client.get('/api/v1/reports/' + record['full_report_id']).json()['data']
        calls = dict(self.fx.model.calls)
        result = self.fx.write('/records/' + record['record_id'], self.payload(record), method='PATCH')['data']
        self.assertEqual(result['version'], record['version'] + 1)
        self.assertEqual(result['manual_values'], {'rent_cent': 355500, 'monthly_cent': 375500, 'initial_cent': 1066500, 'commute_sec': 1800, 'walk_sec': None})
        self.assertEqual(result['remark'], '一起保存的备注')
        self.assertEqual(self.fx.client.get('/api/v1/reports/' + record['full_report_id']).json()['data'], before_report)
        self.assertEqual(dict(self.fx.model.calls), calls)

    def test_map_walking_default_and_manual_clear_keep_provenance_and_precision(self):
        import json
        record = self.candidate()
        report = self.fx.db.one('SELECT * FROM reports WHERE report_id=?', (record['full_report_id'],))
        content = json.loads(report['content']); route = content['route']
        selected = next(x for x in route['routes'] if x['route_id'] == (route.get('selected_route_id') or route.get('adopted_route_id')))
        selected['total_walk_sec'] = 781
        with self.fx.db.transaction() as c:
            c.execute('UPDATE reports SET content=? WHERE report_id=?', (json.dumps(content), report['report_id']))
        record = self.fx.client.get('/api/v1/records/' + record['record_id']).json()['data']
        self.assertEqual(record['current_values']['walk_sec'], 781)
        self.assertEqual(record['value_sources']['walk_sec'], 'map')
        payload = {**self.payload(record), 'commute_sec': record['current_values']['commute_sec'], 'walk_sec': 781}
        saved = self.fx.write('/records/' + record['record_id'], payload, method='PATCH')['data']
        self.assertEqual(saved['current_values']['walk_sec'], 781)
        self.assertEqual(saved['value_sources']['walk_sec'], 'map')
        self.assertIsNone(saved['manual_values']['walk_sec'])
        saved = self.fx.write('/records/' + record['record_id'], {**self.payload(saved), 'walk_sec': 960}, method='PATCH')['data']
        self.assertEqual(saved['current_values']['walk_sec'], 960)
        self.assertEqual(saved['value_sources']['walk_sec'], 'manual')
        self.assertEqual(saved['route_summary']['total_walk_sec'], 781)
        saved = self.fx.write('/records/' + record['record_id'], {**self.payload(saved), 'walk_sec': None}, method='PATCH')['data']
        self.assertIsNone(saved['current_values']['walk_sec'])
        self.assertEqual(saved['value_sources']['walk_sec'], 'manual')
        # A clear is intentional; loading and saving notes must never refill it from the map.
        saved = self.fx.write('/records/' + record['record_id'], self.payload(saved), method='PATCH')['data']
        self.assertIsNone(saved['current_values']['walk_sec'])
        self.assertEqual(self.fx.client.get('/api/v1/records/' + record['record_id']).json()['data'], saved)

    def test_legacy_preference_only_adjustment_does_not_hide_map_walking(self):
        record = self.candidate()
        with self.fx.db.transaction() as c:
            c.execute("INSERT INTO record_manual_adjustments VALUES(?,NULL,'{}','old')", (record['record_id'],))
        value = self.fx.client.get('/api/v1/records/' + record['record_id']).json()['data']
        self.assertEqual(value['current_values']['walk_sec'], value['route_summary']['total_walk_sec'])
        self.assertEqual(value['value_sources']['walk_sec'], 'map')

    def test_invalid_or_conflicting_save_changes_none_of_the_fields(self):
        record = self.candidate()
        payload = self.payload(record)
        payload['monthly_cent'] = -1
        self.fx.write('/records/' + record['record_id'], payload, method='PATCH', expected=422)
        current = self.fx.client.get('/api/v1/records/' + record['record_id']).json()['data']
        self.assertEqual(current, record)
        payload = self.payload(record)
        self.fx.write('/records/' + record['record_id'], payload, method='PATCH')
        payload['remark'] = '过期版本不可覆盖'
        self.fx.write('/records/' + record['record_id'], payload, method='PATCH', expected=409)
        current = self.fx.client.get('/api/v1/records/' + record['record_id']).json()['data']
        self.assertEqual(current['remark'], '一起保存的备注')

    def test_all_group_requires_every_field_and_can_clear_unknowns(self):
        record = self.candidate()
        payload = self.payload(record)
        del payload['commute_sec']
        self.fx.write('/records/' + record['record_id'], payload, method='PATCH', expected=422)
        payload = {**self.fx.command(record['version']), 'group': 'all', 'rent_cent': None, 'monthly_cent': None,
                   'initial_cent': None, 'commute_sec': None, 'remark': ''}
        result = self.fx.write('/records/' + record['record_id'], payload, method='PATCH')['data']
        self.assertTrue(all(x is None for x in result['manual_values'].values()))
        self.assertEqual(result['remark'], '')

    def test_record_times_and_preferences_do_not_change_profile_report_or_screening(self):
        record = self.candidate()
        profile = self.fx.client.get('/api/v1/workspace').json()['data']['profile']
        report = self.fx.client.get('/api/v1/reports/' + record['full_report_id']).json()['data']
        calls = dict(self.fx.model.calls)
        preference = record['preferences'][0]
        payload = {**self.payload(record), 'walk_sec': 900,
                   'preference_results': [{'preference_id': preference['preference_id'], 'result': 'fail'}]}
        saved = self.fx.write('/records/' + record['record_id'], payload, method='PATCH')['data']
        self.assertEqual(saved['version'], record['version'] + 1)
        self.assertEqual(saved['manual_values']['walk_sec'], 900)
        self.assertEqual(saved['preferences'][0]['result'], 'fail')
        self.assertTrue(saved['preferences'][0]['manually_updated'])
        self.assertEqual(saved['preferences'][0]['original_result'], preference['original_result'])
        self.assertEqual(saved['list_status'], 'candidate')
        self.assertEqual(saved['conditions'], record['conditions'])
        self.assertEqual(self.fx.client.get('/api/v1/workspace').json()['data']['profile'], profile)
        self.assertEqual(self.fx.client.get('/api/v1/reports/' + record['full_report_id']).json()['data'], report)
        self.assertEqual(dict(self.fx.model.calls), calls)
        self.assertEqual(self.fx.client.get('/api/v1/records/' + record['record_id']).json()['data'], saved)
        # Old clients omit the new fields: preserve the saved adjustments.
        saved = self.fx.write('/records/' + record['record_id'], self.payload(saved), method='PATCH')['data']
        self.assertEqual(saved['manual_values']['walk_sec'], 900)
        self.assertEqual(saved['preferences'][0]['result'], 'fail')
        cleared = self.fx.write('/records/' + record['record_id'], {**self.payload(saved), 'walk_sec': None,
                               'preference_results': [{'preference_id': preference['preference_id'], 'result': 'unknown'}]}, method='PATCH')['data']
        self.assertIsNone(cleared['manual_values']['walk_sec'])
        self.assertEqual(cleared['preferences'][0]['result'], 'unknown')

    def test_invalid_preference_or_walking_rolls_back_entire_card(self):
        record = self.candidate()
        pref = {'preference_id': record['preferences'][0]['preference_id'], 'result': 'pass'}
        for extra in ({'walk_sec': -1}, {'preference_results': [pref, pref]},
                      {'preference_results': [{'preference_id': 'another-profile-preference', 'result': 'fail'}]},
                      {'preference_results': [{**pref, 'result': 'invalid'}]}):
            self.fx.write('/records/' + record['record_id'], {**self.payload(record), **extra}, method='PATCH', expected=422)
            self.assertEqual(self.fx.client.get('/api/v1/records/' + record['record_id']).json()['data'], record)

    def test_archive_all_includes_unloaded_over_100_and_isolates_other_profiles(self):
        record = self.candidate(notes=132)
        profile = self.fx.workspace['profile']
        original = self.fx.db.one('SELECT * FROM records WHERE record_id=?', (record['record_id'],))
        posts = self.fx.db.all('SELECT post_key FROM posts WHERE post_key!=? ORDER BY post_key LIMIT 131', (original['post_key'],))
        stamp = original['created_at']
        with self.fx.db.transaction() as connection:
            for index, post in enumerate(posts):
                status = 'candidate' if index < 129 else ('archived' if index == 129 else 'screened')
                connection.execute('INSERT INTO records(record_id,profile_id,post_key,list_status,created_at,candidate_added_at) VALUES(?,?,?,?,?,?)',
                                   ('archive-test-' + str(index), profile['profile_id'], post['post_key'], status, stamp, stamp))
            connection.execute('INSERT INTO profiles(profile_id,owner_hash,import_id,state,target_snapshot,created_at) VALUES(?,?,?,?,?,?)',
                               ('other-profile', 'other-owner', self.fx.db.one('SELECT import_id FROM profiles WHERE profile_id=?', (profile['profile_id'],))['import_id'], 'draft', '{}', stamp))
            connection.execute('INSERT INTO records(record_id,profile_id,post_key,list_status,created_at) VALUES(?,?,?,?,?)',
                               ('other-record', 'other-profile', original['post_key'], 'candidate', stamp))
        payload = {**self.fx.command(), 'confirm': True}
        result = self.fx.write('/records/archive-all', payload, key='archive-all-once')['data']
        self.assertEqual(result['archived_count'], 130)
        self.assertEqual(self.fx.db.one("SELECT COUNT(*) n FROM records WHERE profile_id=? AND list_status='candidate'", (profile['profile_id'],))['n'], 0)
        self.assertEqual(self.fx.db.one('SELECT list_status FROM records WHERE record_id=?', ('other-record',))['list_status'], 'candidate')
        self.assertEqual(self.fx.db.one('SELECT list_status FROM records WHERE record_id=?', ('archive-test-130',))['list_status'], 'screened')
        replay = self.fx.write('/records/archive-all', payload, key='archive-all-once')['data']
        self.assertEqual(replay, result)
        self.assertEqual(self.fx.write('/records/archive-all', payload)['data']['archived_count'], 0)
        self.assertIsNotNone(self.fx.db.one('SELECT report_id FROM reports WHERE report_id=?', (record['full_report_id'],)))
