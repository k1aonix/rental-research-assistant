"""One HTML with inline demo data and a runtime-only API key input."""
import ast
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BACKEND = ROOT / 'app/backend'
sys.path.insert(0, str(BACKEND))
from rental.schemas import MODEL_SCHEMAS
from rental.assistant_schemas import CommunicationResult, ContractResult, OCRResult

def js(value):
    return json.dumps(value, ensure_ascii=False).replace('<', '\\u003c')

def build():
    source = (BACKEND / 'tests/build_layout_preview.py').read_text(encoding='utf-8')
    namespace = {'__file__': str(BACKEND / 'tests/build_layout_preview.py')}
    exec(compile(source.split('    encoded =', 1)[0] + '    return data\n', '<fixtures>', 'exec'), namespace)
    namespace['OUT'] = HERE / 'dist'
    data = namespace['build']()
    data['files'],data['tasks'],data['demo_documents'] = {},{},{}
    data['contractFlowVersion']=2
    for r in data['records']:
        r.update(list_status='candidate',screening_reason=None,resume_stage=None,source_url='',remark='虚构演示房源，可自由修改和保存。',disposition='completed')
        report = data['reports'][r['full_report_id'] or r['latest_brief_id']]
        report['kind'] = 'full'
        r['full_report_id'] = report['report_id']
        r['latest_brief_id'] = None
        report['content']['summary'] = '演示样例：' + report['content']['summary']
        report['content']['route']['reference_date'] = '2026-11-23'
        for p in r['preferences']:
            if p['requirement']=='must' and p['result']=='fail':p.update(result='unknown',original_result='unknown')
        rid = r['record_id']
        # Keep the fictional document available, but do not upload it initially.
        contract_text = f'房屋租赁合同（虚构演示）\n房屋：深圳市{r["title"]}一室一厅。\n月租金：{r["manual_values"]["rent_cent"]//100}元。押一付一。\n租期：2026年12月1日至2027年11月30日。\n退租后结清费用且房屋无损坏，出租方应在7日内退还押金。\n正常使用造成的设备故障由出租方维修。提前退租需提前30日通知，具体责任双方协商。\n物业费、水电费标准及出租授权应签约前书面核实。'
        import base64
        fid = 'demo-contract-'+rid
        data['demo_documents'][rid] = dict(file_id=fid,record_id=rid,file_name='虚构租赁合同.txt',content_base64=base64.b64encode(contract_text.encode()).decode(),pages=[{'page':1,'text':contract_text}],text=contract_text)
        for kind in ['communication','contract']:
            key = kind+':'+rid
            draft = data['modules'][key]
            if kind=='contract':
                draft={'background':'','scans':[]}
            else:
                draft['recognized_topic'] = '预置演示样例 · 看房、费用与出租授权'
            data['modules'][key] = {'version':0,'data':draft,'job':None}
    for index,r in enumerate(data['records']):
        r.update(demo_round=1 if index<3 else 2,demo_candidate=True)
    for index,title in enumerate(['学府雅苑两房出租','龙翔花园整租','大学城公寓直租','锦绣家园一室出租','龙岗馨苑整租']):
        r=deepcopy(data['records'][index])
        rid=f'demo-extra-{index+1}'
        r.update(record_id=rid,note_id=f'fixture-extra-{index+1}',title=title,demo_round=1 if index<2 else 2,
                 demo_candidate=False,list_status=None,full_report_id=None,latest_brief_id=f'demo-extra-report-{index+1}',
                 screening_reason='演示样例：月租超过当前预算，未进入候选。',risk_count=0,risk_done=0)
        r['manual_values']['rent_cent']=600000+index*10000
        data['reports'][r['latest_brief_id']]={'report_id':r['latest_brief_id'],'record_id':rid,'kind':'brief',
            'generated_at':'2026-11-23T08:30:00+08:00','content':{'summary':r['screening_reason'],'reason_text':r['screening_reason']}}
        data['records'].append(r)
    task=data['task']
    task.update(task_id='demo-welcome-flow',state='completed',row_ids=[r['record_id'] for r in data['records']],record_results=[])
    task['result']={'outcome':'completed','summary':{'candidate_added':5,'screened':5,'technical_failure':0,'skipped':0,'needs_location':0}}
    for counter,count in zip(task['counters'].values(),[3,2]):
        counter.update(eligible_count=5,attempt_count=5,hit_count=5,candidate_added_count=count,business_screened_count=5-count,reused_count=0)
    task['stage_progress']=[dict(stage=s,label=l,state='succeeded',reason='已完成并保存',steps={'body':'succeeded','map':'succeeded','comments':'succeeded'} if s=='basics' else {}) for s,l in [('classification','帖子分类'),('basics','正文、通勤与原帖评论'),('author','作者调查'),('community','小区调查'),('preference','偏好专项'),('summary','综合判断与报告')]]
    for r in data['records']:
        task['record_results'].append(dict(title=r['title'],note_id=r['note_id'],round=r['demo_round'],outcome='completed' if r['demo_candidate'] else 'screened',reason='虚构演示样例已进入候选' if r['demo_candidate'] else r['screening_reason'],completed_steps=[s['stage'] for s in task['stage_progress']],stage_progress=task['stage_progress']))
    data['tasks'][task['task_id']]=task
    # Begin with the same editable, empty profile shown in the production UI.
    # Keep the five sample candidates ready for the later pages.
    data['demo_target_snapshot']=dict(data['workspace']['profile']['target_snapshot'])
    data['workspace']['profile'].update(state='draft',target_confirmed_at=None,target_snapshot=None,
        preferences=[data['workspace']['profile']['preferences'][0]])
    data['workspace'].update(recent_tasks=[],current_task_id=None)
    rules_ast=ast.parse((BACKEND/'rental/providers.py').read_text(encoding='utf-8'))
    instructions=next(ast.literal_eval(n.value) for n in ast.walk(rules_ast) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='stage_instructions' for t in n.targets))
    schemas=dict(MODEL_SCHEMAS,communication=CommunicationResult,contract=ContractResult,contract_ocr=OCRResult)
    rules={k:{'instruction':instructions[k],'schema':v.model_json_schema()} for k,v in schemas.items() if k in instructions}
    config_file=HERE/'model-config.local.json'
    config=json.loads(config_file.read_text(encoding='utf-8-sig')) if config_file.exists() else {'apiKey':'','model':'z-ai/glm-5.3-flash'}
    # Never copy a local credential into a distributable HTML file.
    config={'apiKey':'','model':config.get('model') or 'z-ai/glm-5.3-flash','guidedDemo':True}
    ga_config=json.loads((HERE/'analytics-config.json').read_text(encoding='utf-8-sig'))
    web=BACKEND/'web'
    app=(web/'app.js').read_text(encoding='utf-8').replace('rent-v214-language','rent-singlehtml-language')
    app=app.replace('高德','模拟地图').replace('查看真实处理记录','查看演示处理记录')
    app=app.replace("async function rpc(path, method='GET', payload=null) {", "async function rpc(path, method='GET', payload=null) {\n  window.rentalAnalytics?.request(path,method,payload);")
    app=app.replace("else window.rentalAnalytics?.emit('operation_failed',{operation_type:method==='GET'?'read':'write',http_status:response.status});", "else window.rentalAnalytics?.failure(path,method,data.error?.code);")
    app=app.replace("catch(error){throw new Error('网络连接中断", "catch(error){window.rentalAnalytics?.failure(path,method,'network');throw new Error('网络连接中断")
    app=app.replace("await navigator.clipboard.writeText(input.value);toast('已复制');", "await navigator.clipboard.writeText(input.value);if(field==='reply')window.rentalAnalytics?.copied();toast('已复制');")
    app=app.replace('return `<div class="card"><div class="card-title"><h2>${esc(a.status)}', 'return `<div class="card" data-ga-contract-result><div class="card-title"><h2>${esc(a.status)}')
    app=app.replace('查询预先准备的原始资料，按需进行真实调查。','使用内置虚构资料模拟分析；不填模型 Key 也可完整体验流程。')
    app=app.replace('仅分析已收录的帖子。再次录入已调查的帖子会复用原结果。','演示可填任意小红书格式链接，将使用内置虚构房源展示流程，不访问真实帖子。')
    app=app.replace("catch(error){toast(error.message);}", "catch(error){window.rentalAnalytics?.clientFailure(error);toast(error.message);}")
    app=app.replace('boot();setInterval(poll,900);', """boot();setInterval(poll,900);
window.addEventListener('rental-demo-progress',event=>{const task=event.detail;if(state.task?.task_id!==task.task_id)return;state.task=clone(task);if(state.workspace?.recent_tasks){const i=state.workspace.recent_tasks.findIndex(t=>t.task_id===task.task_id);if(i>=0)state.workspace.recent_tasks[i]=clone(task);}if(state.page==='input')render();});""")
    # Avoid leaking live service/internal details into the simple demo flow.
    app=app.replace('建议由AI生成，请核对事实后自行发送。','建议回复请核对事实后自行发送。')
    app=app.replace("if(action==='contract-upload')$('#contract-file').click();", """if(action==='contract-upload'){
      await flushModuleSaves();const key=target.dataset.key;
      const result=await rpc(assistantPath(key)+'/demo-upload','POST',{version:state.moduleDrafts[key]?.version||0});
      state.moduleDrafts[key]={...result.data,version:result.version,job:null};render();toast('演示合同已载入，可查看文档并进行AI检查。');
    }""")
    app=app.replace('PDF/DOCX/JPG/PNG；每份或整组10MB，最多20页，图片按选择顺序；文字80000字符；图片逐页识别，无法识别会停止。','演示模式：点击上传合同后，按当前候选生成虚构文档；合同检查在本地完成，无需API Key。')
    app=app.replace("button('AI检查','contract-check'", "button('演示检查','contract-check'")
    css=(web/'styles.css').read_text(encoding='utf-8')+'\n.demo-api-button{margin-left:auto;white-space:nowrap}.topbar #connection{margin-left:0}.demo-api-dialog{width:min(500px,calc(100vw - 32px));padding:24px;border:1px solid #e2e8f0;border-radius:12px}.demo-api-dialog .field{margin:16px 0}.demo-api-dialog .actions{flex-wrap:wrap}'
    html=(web/'index.html').read_text(encoding='utf-8')
    container_id=ga_config.get('container_id','')
    if container_id:
        import re
        if not re.fullmatch(r'GTM-[A-Z0-9]+',container_id):raise ValueError('Invalid GTM container ID')
        bootstrap='''<script id="rental-gtm-bootstrap">
window.dataLayer=window.dataLayer||[];
function gtag(){dataLayer.push(arguments);}
if(location.protocol==='https:'||location.protocol==='http:'){
  gtag('set','send_page_view',false);
  var rentalLanding=new URL(location.origin+location.pathname);
  ['utm_source','utm_medium','utm_campaign','utm_content','utm_term'].forEach(function(key){var value=new URL(location.href).searchParams.get(key);if(value&&/^[a-z0-9_-]{1,64}$/.test(value))rentalLanding.searchParams.set(key,value);});
  gtag('set','page_location',rentalLanding.href);
  try{gtag('set','page_referrer',document.referrer?new URL(document.referrer).origin+'/':'');}catch(e){}
  window.RENTAL_GTM_ACTIVE=true;
  dataLayer.push({'gtm.start':new Date().getTime(),event:'gtm.js'});
  gtag('js',new Date());
  var rentalGtm=document.createElement('script');rentalGtm.async=true;
  rentalGtm.src='https://www.googletagmanager.com/gtm.js?id=__ID__';
  document.head.appendChild(rentalGtm);
}
</script>'''.replace('__ID__',container_id)
        html=html.replace('<head>','<head>'+bootstrap,1)
    html=html.replace('<title>租房助手</title>','<title>租房助手 · 独立演示</title>')
    html=html.replace('<span id="connection"', '<button id="demo-api-settings" type="button" class="small demo-api-button">模型设置</button><span id="connection"')
    html=html.replace('<link rel="stylesheet" href="/assets/styles.css">','<style>'+css+'</style>').replace('<script src="/assets/analytics.js" defer></script>','').replace('<script src="/assets/app.js" defer></script>','')
    html=html.replace('按你的需求调查房源<br>保留证据，现场核实','按你的需求调查房源<br>保留证据，现场核实<br><small>独立演示 · 虚构数据</small><br><button id="demo-reset" type="button" class="small">重新开始演示</button><span id="demo-proxy-status" class="visually-hidden" role="status"></span>')
    html=html.replace('</body>','''<dialog id="demo-api-dialog" class="demo-api-dialog" aria-labelledby="demo-api-title"><form id="demo-api-form"><h2 id="demo-api-title">OpenRouter 模型设置</h2><p>填入 Key 后，沟通助手直接调用模型。合同检查始终在本地演示，不调用模型。</p><label class="field"><span>OpenRouter API Key</span><input id="demo-api-key" type="password" autocomplete="off" spellcheck="false" placeholder="粘贴 OpenRouter API Key"></label><label class="field"><span>模型名称</span><input id="demo-api-model" type="text" autocomplete="off" spellcheck="false"></label><p class="small muted">Key 仅在当前页面内存中使用，不写入 HTML 或浏览器存储；刷新后需重新输入。</p><p id="demo-api-feedback" role="status"></p><div class="actions end"><button id="demo-api-clear" type="button">清除 Key</button><button id="demo-api-close" type="button">关闭</button><button type="submit" class="primary">应用设置</button></div></form></dialog></body>''')
    adapter=(HERE/'demo-adapter.js').read_text(encoding='utf-8')
    script='window.RENTAL_DEMO_CONFIG='+js(config)+';\nwindow.RENTAL_GA4_CONFIG='+js(ga_config)+';\nwindow.RENTAL_DEMO_RULES='+js(rules)+';\nwindow.RENTAL_DEMO_SEED='+js(data)+';\n'+(HERE/'ga4.js').read_text(encoding='utf-8')+'\n'+adapter+'\n'+app
    html=html.replace('</body>','<script>'+script.replace('</script','<\\/script')+'</script></body>')
    out=HERE/'dist';out.mkdir(exist_ok=True)
    (out/'index.html').write_text(html,encoding='utf-8')
    (HERE/'index.html').write_text(html,encoding='utf-8')
    (HERE/'租房助手_单文件演示.html').write_text(html,encoding='utf-8')
    (out/'demo-build-manifest.json').write_text(json.dumps({'format':'single-html','candidates':5,'database':False,'proxy':False,'key_embedded':bool(config['apiKey']),'model':config['model'],'source_sha256':{n:hashlib.sha256((web/n).read_bytes()).hexdigest() for n in ['app.js','index.html','styles.css']}},ensure_ascii=False,indent=2),encoding='utf-8')
    print('Single HTML ready; candidates=5; key embedded='+str(bool(config['apiKey'])))

if __name__=='__main__':build()
