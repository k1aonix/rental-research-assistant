import {test} from 'node:test';
import assert from 'node:assert/strict';
import worker from './worker.mjs';
const env={ALLOWED_ORIGIN:'https://k1aonix.github.io',DEMO_ENABLED:'true',OPENROUTER_API_KEY:'test-only-not-a-real-key',RATE_LIMITER:{limit:async()=>({success:true})}};
function req(body,origin=env.ALLOWED_ORIGIN){return new Request('https://proxy.test/invoke',{method:'POST',headers:{Origin:origin,'Content-Type':'application/json'},body:JSON.stringify(body)});}
test('fail closed: disabled, missing limiter, hostile origin, unknown stage',async()=>{
 assert.equal((await worker.fetch(req({}),{...env,DEMO_ENABLED:'false'})).status,503);
 assert.equal((await worker.fetch(req({}),{...env,RATE_LIMITER:null})).status,503);
 assert.equal((await worker.fetch(req({},'https://evil.test'),env)).status,403);
 assert.equal((await worker.fetch(req({stage:'constructor',payload:{}}),env)).status,422);
 assert.equal((await worker.fetch(req({stage:'body',payload:{},files:[{name:'x',data:'https://evil.test'}]}),env)).status,422);
});
test('rate limit and body size prevent upstream requests',async()=>{
 assert.equal((await worker.fetch(req({}),{...env,RATE_LIMITER:{limit:async()=>({success:false})}})).status,429);
 assert.equal((await worker.fetch(req({stage:'body',payload:{text:'x'.repeat(110001)}}),env)).status,422);
});
test('fixed model/upstream, key stays server side, schema validated, failure redacted',async()=>{
 const original=globalThis.fetch;let sent;
 try{
 globalThis.fetch=async(url,options)=>{sent={url,options};return Response.json({choices:[{finish_reason:'stop',message:{content:JSON.stringify({type:'rental',reason:'出租'})}}]});};
 let result=await worker.fetch(req({stage:'classification',payload:{title:'样例'},model:'expensive-override',endpoint:'https://evil.test'}),env);
 assert.equal(result.status,200);assert.equal(sent.url,'https://openrouter.ai/api/v1/chat/completions');assert.equal(JSON.parse(sent.options.body).model,'z-ai/glm-5.3-flash');assert.equal(sent.options.headers.Authorization,'Bearer '+env.OPENROUTER_API_KEY);assert.ok(!(await result.text()).includes(env.OPENROUTER_API_KEY));
 globalThis.fetch=async()=>Response.json({choices:[{message:{content:'{}'}}]});result=await worker.fetch(req({stage:'classification',payload:{}}),env);assert.equal(result.status,502);
 globalThis.fetch=async()=>new Response('secret-provider-details',{status:402});result=await worker.fetch(req({stage:'classification',payload:{}}),env);assert.equal(result.status,402);assert.ok(!(await result.text()).includes('secret-provider-details'));
 }finally{globalThis.fetch=original;}
});
test('contract references must be present in uploaded original',async()=>{
 const original=globalThis.fetch;
 try{globalThis.fetch=async()=>Response.json({choices:[{message:{content:JSON.stringify({status:'需协商',summary:'样例',recognized_summary:{rent:'3000'},sources:{rent:'伪造引用'},pre_signing_questions:[],clause_notices:[],disclaimer:'仅供辅助'})}}]});
 assert.equal((await worker.fetch(req({stage:'contract',payload:{document:'租金3200元'}}),env)).status,502);
 }finally{globalThis.fetch=original;}
});
