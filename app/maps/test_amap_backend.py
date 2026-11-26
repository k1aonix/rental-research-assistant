import json
import unittest
from pathlib import Path
from amap_backend import AmapClient, AmapError, metro_options, public_transport_options, select_route, walking_options

CONTEXT = {'reference_date':'2026-11-17','departure_time':'08:30','timezone':'Asia/Shanghai','direction':'to_target'}

def walking(duration=1800):
    return {'status':'1','route':{'paths':[{'duration':str(duration),'distance':'2000','steps':[]}]}}

def transit(total=1800, walks=(180,120,300), types=('普通公交线路','地铁线路')):
    segments=[]
    for i, seconds in enumerate(walks):
        segments.append({'walking':{'duration':str(seconds),'distance':'200','steps':[{'duration':'999'}]},
                         'bus':{'buslines':[{'type':types[i], 'name':'线路'+str(i), 'departure_stop':{'name':'上车站'},'arrival_stop':{'name':'下车站'}}] if i<len(types) else []},
                         'railway':{'via_stops':[],'spaces':[]},'taxi':[]})
    return {'status':'1','route':{'transits':[{'duration':str(total),'walking_distance':'650','segments':segments}]}}

def joined(*responses):
    return {'status':'1','route':{'transits':[route for response in responses for route in response['route']['transits']]}}

class MapRulesTest(unittest.TestCase):
    def test_bus_metro_and_mixed_allowed_for_properties(self):
        for types, mode in [(('普通公交线路',),'bus'),(('地铁线路',),'metro'),(('普通公交线路','地铁线路'),'bus_metro')]:
            response=transit(walks=(100,100) if len(types)==1 else (100,100,100),types=types)
            option=public_transport_options(response)[0]
            self.assertEqual(option['mode'],mode)
            self.assertEqual(option['transfer_count'],len(types)-1)

    def test_all_walking_segments_without_double_counting(self):
        option=public_transport_options(transit())[0]
        self.assertEqual(option['duration_sec'],1800)
        self.assertEqual(option['total_walk_sec'],600)
        self.assertEqual(option['walking_distance_m'],650)
        result=select_route(walking(4000),transit(),1800,600)
        self.assertIsNotNone(result['selected_route_id'])

    def test_joint_filter_then_shortest(self):
        response=joined(transit(1500,(900,0),('普通公交线路',)),transit(2100,(480,0),('地铁线路',)),transit(1800,(540,0),('普通公交线路',)))
        result=select_route(walking(5000),response,2400,600)
        chosen=next(item for item in result['routes'] if item['route_id']==result['selected_route_id'])
        self.assertEqual(chosen['duration_sec'],1800)

    def test_cross_route_no_joint_solution(self):
        response=joined(transit(1500,(900,0),('普通公交线路',)),transit(2100,(480,0),('地铁线路',)))
        result=select_route(walking(4000),response,1800,600)
        self.assertIsNone(result['selected_route_id'])
        self.assertFalse(any(item['joint_pass'] for item in result['routes']))

    def test_pure_walk_is_subject_to_both_limits(self):
        empty={'status':'1','route':{'transits':[]}}
        self.assertIsNone(select_route(walking(1800),empty,1800,600)['selected_route_id'])
        self.assertIsNotNone(select_route(walking(1800),empty,1800,1800)['selected_route_id'])
        self.assertEqual(walking_options(walking(1800))[0]['total_walk_sec'],1800)

    def test_zero_walk_and_missing_duration_distinct(self):
        response=transit(600,(0,0),('地铁线路',))
        self.assertIsNotNone(select_route(walking(4000),response,1800,0)['selected_route_id'])
        del response['route']['transits'][0]['segments'][0]['walking']['duration']
        self.assertEqual(public_transport_options(response),[])
        for invalid in ('', [], '1.5', -1, True, None):
            response=transit()
            response['route']['transits'][0]['segments'][1]['walking']['duration']=invalid
            self.assertEqual(public_transport_options(response),[])

    def test_unknown_distance_does_not_become_zero(self):
        response=transit()
        del response['route']['transits'][0]['walking_distance']
        self.assertIsNone(public_transport_options(response)[0]['walking_distance_m'])

    def test_disallowed_rail_taxi_and_unknown_traffic_type(self):
        response=transit()
        response['route']['transits'][0]['segments'][0]['railway']['name']='城际列车'
        self.assertEqual(public_transport_options(response),[])
        response=transit()
        response['route']['transits'][0]['segments'][0]['taxi']={'duration':'30'}
        self.assertEqual(public_transport_options(response),[])
        self.assertEqual(public_transport_options(transit(1800,(100,100),('未知交通类型',))),[])

    def test_station_filter_is_separate_and_ignores_property_walk_limit(self):
        def transport(endpoint,params):
            return walking(6000) if endpoint.endswith('walking') else transit(1800,(1200,0),('地铁线路',))
        client=AmapClient('fixture',transport=transport)
        catalog={'stations':[{'id':'station','location':'one'}],'coverage':{'complete':True}}
        result=client.station_plan(catalog,'target',1800,planning_context=CONTEXT)
        self.assertEqual(result['station_budget_sec'],1800)
        self.assertEqual(len(result['stations']),1)
        self.assertEqual(metro_options(transit()),[]) # Mixed bus excluded from station discovery only.

    def test_all_strategies_share_frozen_time_and_no_early_exit(self):
        calls=[]
        def transport(endpoint,params):
            calls.append(params)
            return transit(1800,(0,0),('地铁线路',))
        response=AmapClient('fixture',transport=transport).transit('origin','target',planning_context=CONTEXT)
        self.assertEqual([item['strategy'] for item in calls],[0,1,2,3])
        self.assertTrue(all(item['date']=='2026-11-17' and item['time']=='08:30' and item['origin']=='origin' and item['destination']=='target' for item in calls))
        self.assertEqual(len(response['route']['transits']),4)
        result=select_route(walking(9000),response,2400,600)
        self.assertEqual(len([item for item in result['routes'] if item['mode']=='metro']),1)

    def test_invalid_planning_context_rejected(self):
        client=AmapClient('fixture',transport=lambda *a:self.fail('must not send request'))
        for context in ({},{**CONTEXT,'departure_time':'08:00'},{**CONTEXT,'direction':'return'},{**CONTEXT,'reference_date':'2026-11-21'}):
            with self.assertRaises(AmapError):
                client.transit('a','b',planning_context=context)
        with self.assertRaises(ValueError):
            client.station_plan({},'target',0,planning_context=CONTEXT)

    def test_retry_transient_but_not_auth_daily_quota_or_ban(self):
        replies=iter([{'status':'0','infocode':'10016'},{'status':'1'}])
        waits=[]
        client=AmapClient('secret',transport=lambda *a:next(replies),sleep=waits.append)
        self.assertEqual(client.request('test')['status'],'1')
        self.assertEqual(waits,[1])
        for code in ('10001','10003','10026'):
            calls=[]
            with self.assertRaises(AmapError) as error:
                AmapClient('secret',transport=lambda *a:calls.append(1) or {'status':'0','infocode':code}).request('test')
            self.assertEqual(len(calls),1)
            self.assertNotIn('secret',str(error.exception))

    def test_empty_page_cannot_reset_catalog_total(self):
        replies=iter([{'status':'1','count':'459','pois':[{'id':'a'}]},{'status':'1','count':'0','pois':[]}])
        result=AmapClient('fixture',transport=lambda *a:next(replies))._station_partition('深圳',20)
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['coverage']['reported_count'],459)

    def test_district_fallback_coverage(self):
        def transport(endpoint,params):
            if endpoint=='config/district':
                return {'status':'1','districts':[{'districts':[{'adcode':'440307','level':'district'}]}]}
            if params['city']=='深圳':
                return {'status':'1','count':'2','pois':[{'id':'a'}] if params['page']==1 else []}
            return {'status':'1','count':'2','pois':[{'id':'a'},{'id':'b'}]}
        catalog=AmapClient('fixture',transport=transport).station_catalog()
        self.assertTrue(catalog['coverage']['complete'])
        self.assertEqual(len(catalog['stations']),2)

    def test_real_recorded_bus_routes_are_now_property_candidates(self):
        root=Path(__file__).resolve().parents[2]
        data=json.loads((root/'程序/后端/tests/fixtures/历史验收/地图验证/amap_route_result_20261116_120002.json').read_text(encoding='utf-8-sig'))
        response=next(item['response'] for item in data['calls'] if item['test']=='公共交通路线')
        self.assertEqual(metro_options(response),[])
        self.assertEqual(len(public_transport_options(response)),2)
        self.assertTrue(all(item['mode']=='bus' for item in public_transport_options(response)))

    def test_v137_live_responses_replay_without_network(self):
        from amap_live_check import verify_report
        root = Path(__file__).resolve().parents[2]
        report = json.loads((root/'程序/后端/tests/fixtures/历史验收/地图验证/地图接口V1.37实时验证.json').read_text(encoding='utf-8'))
        self.assertTrue(verify_report(report)['passed'])
        origin = next(row['params']['origin'] for row in report['calls'] if row['endpoint'] == 'direction/walking')
        destination = next(row['params']['destination'] for row in report['calls'] if row['endpoint'] == 'direction/walking')
        def replay(endpoint, params):
            return next(row['response'] for row in report['calls'] if row['endpoint'] == endpoint and row['params'] == params)
        client = AmapClient('offline-replay-only', transport=replay)
        result = client.evaluate(origin, destination, 2400, 600, planning_context=report['planning_context'])
        self.assertEqual(result['routes'], report['property_evaluation']['routes'])
        self.assertIsNone(result['selected_route_id'])
        fastest = min((row for row in result['routes'] if row['mode'] == 'bus'), key=lambda row: row['duration_sec'])
        self.assertEqual((fastest['duration_sec'], fastest['total_walk_sec']), (1659, 1022))
        report['calls'][0]['status'] = '0'
        self.assertFalse(verify_report(report)['passed'])

if __name__=='__main__':
    unittest.main(verbosity=2)
