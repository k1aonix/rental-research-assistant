"""Create an explicitly labelled, self-contained visual preview; never imported by production."""
import json
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / 'app/backend/web'
LEGACY = ROOT / 'app/demo/latest/fixtures'
OUT = ROOT / 'app/demo/latest/preview'


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    fixture = json.loads((LEGACY / 'fixtures.json').read_text(encoding='utf-8'))
    documents = json.loads((LEGACY / 'contract-documents.json').read_text(encoding='utf-8'))
    notes = {n['note_id']: n for n in fixture['notes']}
    preferences = [
        {'preference_id': 'preview-agent', 'kind': 'agent', 'original_text': '拒绝中介及二房东', 'requirement': 'must'},
        {'preference_id': 'preview-quiet', 'kind': 'custom', 'original_text': '安静', 'requirement': 'flexible'},
        {'preference_id': 'preview-light', 'kind': 'custom', 'original_text': '采光好', 'requirement': 'flexible'}]
    profile = {'profile_id': 'layout-preview-profile', 'state': 'locked', 'version': 1,
               'min_rent_cent': 260000, 'max_rent_cent': 400000, 'max_commute_min': 45, 'max_walk_min': 20,
               'preferences': preferences, 'target_confirmed_at': '2026-11-18T08:00:00+08:00',
               'target_snapshot': {'target_name': '香港中文大学（深圳）', 'target_address': '深圳市龙岗区龙翔大道2001号',
                                   'target_poi': 'PREVIEW-ONLY', 'location': '114.212820,22.688319', 'city': '深圳市'}}
    limits = {'quota_near_target': 5, 'quota_near_metro': 5, 'max_attempts_near_target': 10, 'max_attempts_near_metro': 10}
    workspace = {'profile': profile, 'fixed_config': {**profile['target_snapshot'], 'config_hash': 'preview', 'target_name': '香港中文大学（深圳）'},
                 'search_defaults': limits, 'active_tasks': [], 'csrf_token': 'local-layout-only'}
    records, reports, viewings, evidence, modules = [], {}, {}, {}, {}
    general = [{'key': x['check_id'], 'category': x['category'], 'title': x['title'],
                'how_to_check': x['how_to_check'], 'abnormal_signal': x['abnormal_signal']} for x in fixture['generic_site_checks']]
    for index, (code, status) in enumerate([('W01', 'candidate'), ('M01', 'candidate'), ('U04', 'candidate'),
                                           ('M05', 'archived'), ('W03', 'screened')]):
        note = notes['fixture-' + code]
        source = deepcopy(note.get('resume_fixture', {}).get('full_report') or note['preset_analysis'])
        rid, report_id = 'preview-' + code, 'preview-report-' + code
        route = deepcopy(note['route_fixture']['routes'][0])
        route.update(commute_pass=True, walk_pass=True, joint_pass=True,
                     segments=[{'mode': 'walk', 'duration_sec': route['total_walk_sec'], 'distance_m': route['walking_distance_m']}])
        if route['mode'] != 'walk':
            route['segments'].append({'mode': route['mode'], 'line_name': '路线示意线路',
                                      'departure_stop': {'name': '小区附近站'}, 'arrival_stop': {'name': '港中深附近站'}})
        route_context = {'reference_date': '2026-11-19', 'departure_time': '08:30', 'timezone': 'Asia/Shanghai',
                         'queried_at': '2026-11-18T08:20:00+08:00', 'selected_route_id': route['route_id'],
                         'routes': [route], 'commute_limit_sec': 2700, 'walk_limit_sec': 1200}
        risks = [{'risk_id': x['risk_id'], 'title': x['text'], 'reason': x.get('evidence_summary', ''),
                  'verification_hint': x.get('question') or x['text'],
                  'evidence_ids': x.get('evidence_note_ids', []) + x.get('author_history_refs', [])}
                 for x in source.get('risk_items', [])]
        pref_results = []
        for p in preferences:
            prior = next((x for x in source.get('preference_results', []) if x['preference'] == p['original_text']), {})
            pref_results.append({'preference_id': p['preference_id'], 'result': {'符合': 'pass', '不符合': 'fail'}.get(prior.get('result'), 'unknown'),
                                 'reason': prior.get('reason', '资料不足，现场核实'),
                                 'evidence_ids': prior.get('evidence_note_ids', []) + prior.get('author_history_refs', [])})
        report_summary = '；'.join(p['original_text'] + '：' + {'pass': '符合', 'fail': '不符合', 'unknown': '无法确认'}[v['result']]
                                  for p, v in zip(preferences, pref_results))
        content = {'summary': report_summary, 'preferences': pref_results, 'risks': risks,
                   'body': {'community_name': note.get('resolved_community_name') or note.get('community_hint'),
                            'city': '深圳市', 'rent_cent': source.get('monthly_rent_yuan', 3200) * 100},
                   'route': route_context, 'evidence_ids': source.get('evidence_note_ids', []),
                   'investigations': [{'stage': 'community', 'result': {'facts': [{'text': x.get('text', ''),
                                         'evidence_ids': x.get('evidence_note_ids', [])}], 'conflicts': [], 'missing': []}}
                                      for x in source.get('report_sections', []) if x.get('section') == '小区总体评价']}
        reports[report_id] = {'report_id': report_id, 'record_id': rid, 'kind': 'brief' if status == 'screened' else 'full',
                              'generated_at': '2026-11-18T08:20:00+08:00', 'content': content}
        rent = source.get('monthly_rent_yuan') or 3200
        record = {'record_id': rid, 'profile_id': profile['profile_id'], 'list_status': status,
                  'version': 1, 'note_id': note['note_id'], 'title': note.get('resolved_community_name') or note.get('community_hint') or note['title'],
                  'source_url': note['source_url'], 'published_at': '2026-11-16T12:00:00+08:00',
                  'community_supplement': None, 'remark': '已预约周末看房，重点核实授权、噪音和实际费用。',
                  'manual_values': {'rent_cent': rent * 100, 'monthly_cent': (rent + 200) * 100,
                                    'initial_cent': rent * 300, 'commute_sec': route['duration_sec'] + 120, 'walk_sec': route['total_walk_sec']},
                  'route_summary': {**route, **{k: route_context[k] for k in ('reference_date', 'queried_at')}},
                  'full_report_id': report_id if status != 'screened' else None,
                  'latest_brief_id': report_id if status == 'screened' else None,
                  'risk_count': len(risks), 'risk_done': 1 if risks else 0, 'viewing_conclusion': 'undecided',
                  'summary': report_summary, 'conditions': [], 'preferences': [{**p, 'result': result['result'], 'original_result': result['result'], 'manually_updated': False} for p, result in zip(preferences, pref_results)],
                  'screening_reason': '综合证据提示发布者疑似中介，不符合必须满足的身份偏好。' if status == 'screened' else None,
                  'resume_stage': 'summary' if status == 'screened' else None}
        if code == 'U04':
            record['preferences'][1].update(result='fail', manually_updated=True)
        records.append(record)
        viewings[rid] = {'record_id': rid, 'version': 1, 'conclusion': record['viewing_conclusion'],
                         'summary': '已核对朝向与采光，晚间噪音及签约授权仍需补充核实。',
                         'followup': '请提供产权或有效出租授权；确认物业费及退租条件。',
                         'risk_checks': {x['risk_id']: i == 0 for i, x in enumerate(risks)},
                         'general_checks': {x['key']: i < 4 for i, x in enumerate(general)},
                         'template': {'template_version': 'layout-v1', 'risk_items': risks, 'general_items': general}}
        reply = fixture['preset_replies'][note['note_id']]
        draft = {'original_message': reply['input_owner_message'], 'goal': reply['input_goal'],
                 'style': '柔和', 'generated_style': '柔和', 'preview_replies': reply['by_style'],
                 'reply': reply['by_style']['柔和'], 'recognized_topic': reply.get('recognized_topic', ''),
                 'send_checks': reply.get('send_checks', []), 'remaining_questions': reply.get('remaining_questions', []),
                 'signals': '围绕当前房源的费用、授权与待核实事项沟通，不代表对方已承诺。'}
        draft['history'] = [{**draft, 'created_at': '2026-11-18 16:30', 'session_id': 'preview-conversation-' + code,
                             'summary': '已沟通看房安排，费用与授权信息仍需逐项确认。', 'update_applied': False}]
        draft['turns'] = [
            {'id': 'preview-other-' + code, 'role': 'other', 'text': '你好，房子还在，可以先约时间看房。', 'time': '16:20'},
            {'id': 'preview-self-' + code, 'role': 'self', 'text': '好的，谢谢。我想再确认一下房源费用和出租授权。', 'time': '16:22'}]
        draft['reply'] = ''
        modules['communication:' + rid] = draft
        contract_id = note['contract_fixture_id']
        document = documents[contract_id]
        scan = {'scan_id': 'preview-scan-' + code, 'file_name': document['file_name'], 'created_at': '2026-11-18 16:35',
                'status': fixture['preset_contracts'][contract_id]['preset_scan']['status'],
                'result': fixture['preset_contracts'][contract_id]['preset_scan']}
        modules['contract:' + rid] = {'background': '已约定看房，签约前核对押金退还、维修范围及提前解约责任。',
                                      'upload': {k: document[k] for k in ('file_name', 'page_images', 'pdf_base64')}, 'scans': [scan]}
    for note_id, note in notes.items():
        evidence[note_id] = {'excerpt': (note.get('title', '') + '\n' + note.get('content', '') + '\n' + '\n'.join(
            x.get('text', '') for x in note.get('comments', {}).get('top_level_items', []))),
            'source': {'url': note.get('source_url'), 'note_id': note_id}}
        if note.get('author', {}).get('profile_id'):
            evidence[note['author']['profile_id']] = {'excerpt': json.dumps(note['author'], ensure_ascii=False, indent=2), 'source': {}}
    task = {'task_id': 'layout-search', 'profile_id': profile['profile_id'], 'version': 1, 'mode': 'auto', 'state': 'completed',
            'round': 2, 'current_step': 'summary', 'current_listing': {'note_id': records[2]['note_id'], 'title': records[2]['title']},
            'config_snapshot': {'search': limits}, 'planning_context': {'reference_date': '2026-11-19'},
            'step_states': {k: 'succeeded' for k in ('classification', 'body', 'map', 'comments', 'author', 'community', 'preference', 'summary')},
            'counters': {'near_target': {'eligible_count': 3, 'attempt_count': 7, 'hit_count': 8, 'candidate_added_count': 2, 'reused_count': 1},
                         'near_metro': {'eligible_count': 2, 'attempt_count': 6, 'hit_count': 7, 'candidate_added_count': 2, 'reused_count': 1}},
            'events': [{'at': '2026-11-18T08:20:%02d+00:00' % i, 'step': k, 'type': 'step_succeeded',
                        'data': {'reason': v}} for i, (k, v) in enumerate([
                            ('classification', '已读取原帖并完成帖子分类'), ('body', '正文月租与城市已核对'),
                            ('map', '同一路线总通勤与累计步行均符合'), ('comments', '已整理评论与费用线索'),
                            ('author', '已收集发布人身份线索'), ('community', '已整理小区正反证据'),
                            ('preference', '已收集各项偏好证据'), ('summary', '统一判断后保存报告及候选结果')])],
            'pending_action': None, 'error': None, 'result': {'outcome': 'completed', 'record_ids': [r['record_id'] for r in records]}}
    data = {'workspace': workspace, 'records': records, 'reports': reports, 'viewings': viewings,
            'evidence': evidence, 'modules': modules, 'task': task}
    encoded = json.dumps(data, ensure_ascii=False).replace('<', '\\u003c')
    adapter = r'''
const PREVIEW_DB=__DATA__;
window.RENTAL_LAYOUT_DATA=PREVIEW_DB;
window.rentalLayoutPreview={async generateCommunication(input){
 const sample=PREVIEW_DB.modules['communication:'+input.record_id];
 if(!sample)throw new Error('Preview sample not found');
 return {reply:sample.preview_replies[input.style]||sample.preview_replies['柔和'],recognized_topic:sample.recognized_topic,send_checks:sample.send_checks,remaining_questions:sample.remaining_questions,signals:sample.signals};
}};

window.fetch=async function(url,options={}){
 const u=new URL(url,'http://layout-preview.local'),path=u.pathname.replace('/api/v1',''),method=options.method||'GET',body=JSON.parse(options.body||'{}');
 let data=null,status=200;
 if(path==='/workspace')data=PREVIEW_DB.workspace;
 else if(path==='/records'&&method==='GET')data={items:PREVIEW_DB.records.filter(r=>r.list_status==='candidate'),next_cursor:null};
 else if(path==='/history')data={archived:{items:PREVIEW_DB.records.filter(r=>r.list_status==='archived'),next_cursor:null},screened:{items:PREVIEW_DB.records.filter(r=>r.list_status==='screened'),next_cursor:null}};
 else if(path.startsWith('/reports/'))data=PREVIEW_DB.reports[path.split('/').pop()];
 else if(path.startsWith('/evidence/'))data=PREVIEW_DB.evidence[path.split('/').pop()];
 else if(path.startsWith('/tasks/')&&method==='GET')data=PREVIEW_DB.task;
 else if(path.endsWith('/viewing')){const id=path.split('/')[2];data=PREVIEW_DB.viewings[id];if(method==='PUT'){Object.assign(data,body);data.version++;const r=PREVIEW_DB.records.find(r=>r.record_id===id);r.viewing_conclusion=data.conclusion;r.risk_done=Object.values(data.risk_checks).filter(Boolean).length;data={viewing:data,record:r};}}
 else if(path==='/records/archive-all'){const rows=PREVIEW_DB.records.filter(r=>r.list_status==='candidate');rows.forEach(r=>{r.list_status='archived';r.version++});data={archived_count:rows.length};}
 else if(path==='/records/archive'){for(const item of body.items)PREVIEW_DB.records.find(r=>r.record_id===item.record_id).list_status='archived';data={records:PREVIEW_DB.records.filter(r=>body.items.some(x=>x.record_id===r.record_id))};}
 else if(path.endsWith('/restore')){data=PREVIEW_DB.records.find(r=>r.record_id===path.split('/')[2]);data.list_status='candidate';}
 else if(/^\/records\/[^/]+$/.test(path)){data=PREVIEW_DB.records.find(r=>r.record_id===path.split('/')[2]);if(method==='PATCH'){if(body.group==='remark'||body.group==='all')data.remark=body.remark;if(body.group!=='remark')for(const k of ['rent_cent','monthly_cent','initial_cent','commute_sec','walk_sec'])if(k in body)data.manual_values[k]=body[k];if(body.preference_results)for(const p of body.preference_results){const item=data.preferences.find(x=>x.preference_id===p.preference_id);if(item){item.result=p.result;item.manually_updated=true;}}data.version++;}}
 else {status=409;data={error:{code:'LAYOUT_PREVIEW_ONLY',message:'这是已填充的设计预览，不执行真实查询或调查。刷新页面可恢复初始示例。'}};}
 return new Response(JSON.stringify(status===200?{data}:data),{status,headers:{'Content-Type':'application/json'}});
};
'''.replace('__DATA__', encoded)
    app = (WEB / 'app.js').read_text(encoding='utf-8')
    app = app.replace("async function boot(){try{await reloadWorkspace();", "async function boot(){try{await reloadWorkspace();state.task=structuredClone(window.RENTAL_LAYOUT_DATA.task);state.moduleDrafts=structuredClone(window.RENTAL_LAYOUT_DATA.modules);await loadRecords();state.selected=state.records[0]?.record_id;")
    app = app.replace('高德预计：', '路线示意：').replace('高德去程路线', '去程路线示意')
    app = app.replace("$('#connection').textContent='工作区已连接'", "$('#connection').textContent='设计预览 · 示例数据 · 无需接口'")
    html = (WEB / 'index.html').read_text(encoding='utf-8')
    html = html.replace('<title>租房助手</title>', '<title>租房助手 · 已填充设计预览</title>')
    html = html.replace('<link rel="stylesheet" href="/assets/styles.css">', '<style>' + (WEB / 'styles.css').read_text(encoding='utf-8') + '</style>')
    html = html.replace('<script src="/assets/analytics.js" defer></script>', '')
    html = html.replace('<script src="/assets/app.js" defer></script>', '')
    html = html.replace('</body>', '<script>' + adapter + '\n' + app + '</script></body>')
    (OUT / '租房助手_已填充预览.html').write_text(html, encoding='utf-8')
    (OUT / 'README.md').write_text('''# 已填充设计预览

双击“租房助手_已填充预览.html”，或项目根目录“打开设计预览.cmd”。画像已锁定、房源已录入、看房记录和沟通／合同内容已填充；六个模块均可直接查看。无需高德、模型、后端服务或数据库。

本预览使用与正式页面同一套模板和样式，示例数据仅存在于本文件的内存中，不进入正式后端。刷新恢复初始示例，不覆盖任何真实用户数据。搜索、生成和扫描不执行真实服务；已有结果用于查看布局。

原Demo v2.13保持不变。正式入口仍为“打开新版页面.cmd”。
''', encoding='utf-8')
    launcher = '@echo off\r\nchcp 65001 >nul\r\nstart "" "%~dp0程序\\界面预览\\v2.16\\租房助手_已填充预览.html"\r\n'
    (ROOT / '打开设计预览.cmd').write_bytes(launcher.encode('utf-8'))
    print(OUT / '租房助手_已填充预览.html')


if __name__ == '__main__':
    build()
