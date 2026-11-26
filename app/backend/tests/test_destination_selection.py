import unittest
from unittest.mock import patch

from . import test_system as base
from rental.source import SourceImporter
from rental.providers import MapGateway


class DestinationSelectionTest(unittest.TestCase):
    setUp = base.SystemTest.setUp
    tearDown = base.SystemTest.tearDown
    write = base.SystemTest.write
    refresh = base.SystemTest.refresh
    command = base.SystemTest.command
    drain = base.SystemTest.drain

    def initialize(self):
        data, manifest = base.dataset()
        SourceImporter(self.db).import_data(data, manifest, 'destination-test')
        self.workspace = self.write('/workspace', {})['data']
        self.maps.search_targets = lambda query: [
            {'target_poi': self.settings.target_poi, 'target_name': '香港中文大学（深圳）',
             'target_address': '龙翔大道2001号', 'location': '114.212820,22.688319', 'district': '龙岗区', 'city': '深圳市'},
            {'target_poi': 'OTHER_POI', 'target_name': '深圳其他目的地', 'target_address': '另一地址',
             'location': '114.100000,22.600000', 'district': '福田区', 'city': '深圳市'}]

    def candidates(self):
        response = self.client.get('/api/v1/target-candidates', params={'q': '大学'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['data']['items']

    def select(self, token):
        self.write('/profile/confirm-target', {**self.command(), 'candidate_token': token})
        return self.refresh()

    def profile_payload(self):
        return {**self.command(), 'min_rent_cent': 200000, 'max_rent_cent': 450000,
                'max_commute_min': 40, 'max_walk_min': 10,
                'preferences': [{'kind': 'agent', 'original_text': '拒绝中介及二房东', 'requirement': 'must'}]}

    def test_real_candidates_are_preserved_and_chosen_snapshot_is_used(self):
        self.initialize()
        items = self.candidates()
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0]['data_covered'])
        self.assertFalse(items[1]['data_covered'])
        selected = self.select(items[0]['candidate_token'])
        self.assertEqual(selected['target_snapshot']['location'], '114.212820,22.688319')
        self.write('/profile/save', self.profile_payload())
        self.assertEqual(self.refresh()['state'], 'locked')

    def test_other_destination_is_selectable_but_save_reports_missing_data(self):
        self.initialize()
        selected = self.select(self.candidates()[1]['candidate_token'])
        self.assertEqual(selected['target_snapshot']['target_name'], '深圳其他目的地')
        error = self.write('/profile/save', self.profile_payload(), expected=409)
        self.assertEqual(error['error']['code'], 'SOURCE_COVERAGE_UNAVAILABLE')
        self.assertEqual(self.refresh()['state'], 'draft')
        self.assertEqual(self.refresh()['preferences'], [])

    def test_candidate_coordinates_cannot_be_tampered_with(self):
        self.initialize()
        token = self.candidates()[0]['candidate_token']
        payload, signature = token.rsplit('.', 1)
        changed = payload[0:-1] + ('A' if payload[-1] != 'A' else 'B') + '.' + signature
        error = self.write('/profile/confirm-target', {**self.command(), 'candidate_token': changed}, expected=409)
        self.assertEqual(error['error']['code'], 'TARGET_SELECTION_EXPIRED')
        self.assertIsNone(self.refresh()['target_confirmed_at'])

    def test_budget_accepts_integer_yuan_only(self):
        self.initialize()
        self.select(self.candidates()[0]['candidate_token'])
        payload = self.profile_payload()
        payload['min_rent_cent'] = 200050
        error = self.write('/profile/save', payload, expected=422)
        self.assertEqual(error['error']['code'], 'VALIDATION_FAILED')

    def test_completed_search_keeps_last_listing_and_saved_stage_states(self):
        self.initialize()
        self.select(self.candidates()[0]['candidate_token'])
        self.write('/profile/save', self.profile_payload())
        self.refresh()
        task = self.write('/tasks/search', self.command())['data']
        task = self.drain(task['task_id'])
        self.assertEqual(task['state'], 'completed')
        self.assertIsNotNone(task['current_listing'])
        self.assertEqual(task['step_states']['classification'], 'succeeded')

    def test_amap_gateway_calls_poi_endpoint_and_preserves_ambiguity(self):
        self.initialize()
        self.settings.amap_key = 'isolated-test-only'
        with patch('rental.providers.AmapClient') as client:
            client.return_value.request.return_value = {'pois': [
                {'id': 'ONE', 'name': '地址一', 'address': '位置一', 'location': '114.2,22.6', 'cityname': '深圳市'},
                {'id': 'TWO', 'name': '地址二', 'address': '位置二', 'location': '114.3,22.7', 'adcode': '440307'},
                {'id': 'BAD', 'name': '外地', 'location': '120.0,30.0', 'cityname': '其他市'}]}
            items = MapGateway(self.settings).search_targets('香港中文大学深圳')
            self.assertEqual([x['target_poi'] for x in items], ['ONE', 'TWO'])
            client.return_value.request.assert_called_once_with('place/text', keywords='香港中文大学深圳',
                                                                city='深圳', citylimit='true', offset=10, page=1)
