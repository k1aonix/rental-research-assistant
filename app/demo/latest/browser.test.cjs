const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),http=require('node:http');
let pw;try{pw=require('playwright')}catch{pw=require(path.join(require('node:os').homedir(),'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'))}
const root=path.join(__dirname,'dist'),results=[];
const server=http.createServer(async(req,res)=>{try{const name=decodeURIComponent(req.url.split('?')[0]).replace(/^\/631604rental-demo\//,'');const file=path.resolve(root,name||'index.html');if(!file.startsWith(root+path.sep))throw Error();const data=await fs.readFile(file);res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html');res.end(data);}catch{res.writeHead(404).end();}});
async function run(){await new Promise(r=>server.listen(18521,'127.0.0.1',r));let browser;
 try{browser=await pw.chromium.launch({channel:'msedge',headless:true});const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:18521/631604rental-demo/');await page.locator('#connection').filter({hasText:'独立 Demo'}).waitFor();
 async function rpc(p,method='GET',body){return page.evaluate(async({p,method,body})=>(await fetch('/api/v1'+p,{method,body:body?JSON.stringify(body):undefined})).json(),{p,method,body});}
 for(const width of [1440,390,320]){await page.setViewportSize({width,height:900});for(const nav of ['profile','input','candidates','viewing','communication','contract']){await page.locator('[data-nav='+nav+']').evaluate(el=>el.click());await page.waitForTimeout(100);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${nav} overflow at ${width}`);assert.ok(await page.locator('#app').innerText());}results.push(`six current pages, no overflow at ${width}px`);}
 const rows=(await rpc('/records')).data.items,r=rows[0];assert.ok(rows.length);
 let response=await rpc('/records/'+r.record_id,'PATCH',{version:r.version,group:'all',rent_cent:350000,monthly_cent:370000,initial_cent:700000,commute_sec:2400,walk_sec:600,remark:'demo-only-save',preference_results:[]});assert.equal(response.data.remark,'demo-only-save');
 await page.reload();await page.locator('#connection').filter({hasText:'独立 Demo'}).waitFor();assert.equal((await rpc('/records/'+r.record_id)).data.manual_values.rent_cent,350000);results.push('candidate atomic edit survives browser refresh');
 response=await rpc('/tasks/search','POST',{});assert.equal(response.error.code,'PROXY_NOT_CONFIGURED');results.push('missing proxy never fabricates AI success');
 await page.evaluate(()=>window.RENTAL_DEMO_CONFIG.proxyURL='https://proxy.test');
 const calls=[];
 await page.route('https://proxy.test/invoke',async route=>{const input=route.request().postDataJSON();calls.push(input.stage);let data;
 if(input.stage==='communication')data={reply:'您好，方便先确认费用和出租授权吗？',recognized_topic:'授权',send_checks:['核对'],signals:'未确认身份',remaining_questions:['费用']};
 else if(input.stage==='classification')data={type:'rental',reason:'出租样例'};
 else if(input.stage==='body')data={city:'深圳市',community_name:'虚构小区',rent_cent:320000,monthly_cent:null,initial_cent:null,facts:[]};
 else if(input.stage==='summary')data={summary:'基于虚构资料生成的真实流程测试',facts:[],preferences:input.payload.preferences.map(p=>({preference_id:p.preference_id,result:'unknown',reason:'资料不足',evidence_ids:[]})),conflicts:[],missing:[],risks:[],report_topics:[{section:'housing',topic:'测试主题',conclusion:'虚构资料仅供演示，无法确认真实居住条件。',evidence_ids:[],status:'unknown'}]};
 else data={facts:[],missing:[],conflicts:[]};
 await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({data})});});
 const base='/records/'+r.record_id+'/assistants/communication';let a=(await rpc(base)).data;
 a=(await rpc(base,'POST',{version:a.version,action:'draft',original_message:'请先交定金',goal:'',style:'柔和',reply:''})).data;
 a=(await rpc(base,'POST',{version:a.version,action:'generate',original_message:'请先交定金',goal:'',style:'柔和'})).data;assert.equal(a.data.turns.length,1);assert.ok(a.data.pending);
 a=(await rpc(base,'POST',{version:a.version,action:'confirm',reply:a.data.reply})).data;assert.equal(a.data.turns.length,2);assert.equal(a.data.pending,null);
 a=(await rpc(base,'POST',{version:a.version,action:'end'})).data;assert.equal(a.data.history.length,1);assert.equal(a.data.turns.length,0);results.push('real proxy contract exercised with isolated model: generate, confirm, history');
 const contract='/records/'+r.record_id+'/assistants/contract';const c=(await rpc(contract,'POST',{version:0,action:'draft',background:'演示背景'})).data;
 response=await rpc(contract+'/upload','POST',{version:c.version,file_name:'demo.txt',content_base64:Buffer.from('租金3500元，押一付一。').toString('base64')});assert.ok(response.data.data.upload.file_id);results.push('browser contract text upload and file isolation');
 const t=(await rpc('/tasks/search','POST',{})).data;assert.ok(t.task_id);let finished;
 for(let attempt=0;attempt<100;attempt++){finished=(await rpc('/tasks/'+t.task_id)).data;if(finished.state!=='running')break;await page.waitForTimeout(30);}
 assert.equal(finished.state,'completed',JSON.stringify(finished.error));assert.equal(finished.record_results.length,3);assert.equal(finished.result.summary.candidate_added+finished.result.summary.screened,3);assert.ok(calls.includes('summary'));assert.equal((await rpc('/records/'+r.record_id)).data.manual_values.rent_cent,350000);results.push('model investigation sequence, screening, report totals, manual edits preserved');
 await page.reload();await page.locator('#connection').filter({hasText:'独立 Demo'}).waitFor();await page.locator('[data-nav=input]').evaluate(el=>el.click());await page.waitForTimeout(150);const dialog=page.locator('#search-complete-dialog');if(await dialog.isVisible())await page.locator('[data-action=completion-stay]').click();
 await page.locator('[data-nav=candidates]').evaluate(el=>el.click());await page.locator('[data-action=report]').first().click();await page.locator('#report-dialog').waitFor();assert.ok((await page.locator('#report-dialog').innerText()).includes('测试主题'));await page.locator('[data-action=close-report]').click();results.push('current report narrative UI renders AI report topics');
 assert.deepEqual(errors,[]);await page.setViewportSize({width:1440,height:1000});await page.locator('[data-nav=candidates]').evaluate(el=>el.click());await fs.mkdir(path.join(__dirname,'../../../运行与迁移记录/验收成果/原始记录/Demo回归'),{recursive:true});await page.screenshot({path:path.join(__dirname,'../../../运行与迁移记录/验收成果/原始记录/Demo回归/desktop.png'),fullPage:true});
 await rpc('/records/archive-all','POST',{});assert.equal((await rpc('/records')).data.items.length,0);assert.ok((await rpc('/history')).data.archived.items.length);results.push('archive and history isolated in browser');
 console.log(JSON.stringify(results,null,2));await fs.writeFile(path.join(__dirname,'../../../运行与迁移记录/验收成果/原始记录/Demo回归/results.json'),JSON.stringify({passed:results},null,2));
 }finally{await browser?.close();server.close();}}
run().catch(e=>{console.error(e);process.exitCode=1;server.close();});
