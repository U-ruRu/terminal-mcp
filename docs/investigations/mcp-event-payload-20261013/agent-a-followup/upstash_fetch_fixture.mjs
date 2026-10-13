// Reviewed Upstash function bodies + real Node fetch, local TLS only. No live callback.
import assert from 'node:assert/strict';
import {createHash,createHmac,webcrypto} from 'node:crypto';
import fs from 'node:fs';
import https from 'node:https';
import {spawn,execFileSync} from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
const SOURCE_SHA256='1e4137cd989afccc7fd9bd27df0b1f5c37b585e274a43aae9d863e5d15bc2d4d';
const digest=x=>createHash('sha256').update(x).digest('hex');
if(process.argv[2]==='child') {
  const c=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
  const dest=new URL(c.url);assert.equal(dest.hostname,'127.0.0.1');assert.equal(dest.protocol,'https:');assert.ok(!dest.username&&!dest.password);
  const src=fs.readFileSync(c.source,'utf8');assert.equal(digest(src),SOURCE_SHA256);
  const signHead=src.indexOf('export async function signWebhook('),signBody=src.indexOf('  const key = decodeSecret(secret);',signHead),signEnd=src.indexOf('\n}',signBody);
  const sign=new Function('decodeSecret','crypto','utf8','toBase64',
    'return async function(secret,id,body,timestampSeconds){'+src.slice(signBody,signEnd)+'\n}')(s=>Buffer.from(s.slice(6),'base64'),webcrypto,x=>new TextEncoder().encode(x),x=>Buffer.from(x).toString('base64'));
  const postHead=src.indexOf('export async function postSigned(options:'),postBody=src.indexOf('  const headers = await signWebhook(',postHead),postEnd=src.indexOf('\n}',postBody);
  const post=new Function('signWebhook','fetch','AbortSignal','CALLBACK_TIMEOUT_MS',
    'return async function(options){'+src.slice(postBody,postEnd)+'\n}')(async(s,id,b)=>sign(s,id,b,c.signedAt),fetch,AbortSignal,10000);
  const result=await post({url:c.url,secret:c.secret,webhookId:c.event.eventId,subscriptionId:'sub_fixture',body:JSON.stringify(c.event)});
  assert.equal(result.status,200);await result.text();
  console.log(JSON.stringify({status:result.status,sign_body_sha256:digest(src.slice(signBody,signEnd)),post_body_sha256:digest(src.slice(postBody,postEnd))}));
} else {
  const source=process.argv[2],output=process.argv[3],tmp=fs.mkdtempSync(path.join(os.tmpdir(),'tmcp-events-fetch-'));
  assert.equal(digest(fs.readFileSync(source)),SOURCE_SHA256);
  const key=path.join(tmp,'key.pem'),cert=path.join(tmp,'cert.pem');
  execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1','-keyout',key,'-out',cert],{stdio:'ignore',timeout:15000});
  const signedAt=Math.floor(Date.now()/1000),timestamp=new Date(signedAt*1000).toISOString(),secret='whsec_'+Buffer.from(Array.from({length:32},(_,i)=>i)).toString('base64');
  const event={eventId:'evt_fetch_fixture',name:'message.received',timestamp,data:{server:'fixture',sender:'Fixture-Sender',recipient:'Fixture-Recipient',message_id:'fixture:fetch',text:'Кавычки "тест", перенос\nстроки ✓ 😀',timestamp},cursor:null};
  let capture;
  const server=https.createServer({key:fs.readFileSync(key),cert:fs.readFileSync(cert)},(req,res)=>{
    const chunks=[];req.on('data',c=>chunks.push(c));req.on('end',()=>{
      const raw=Buffer.concat(chunks),h={...req.headers},expected='v1,'+createHmac('sha256',Buffer.from(secret.slice(6),'base64')).update(h['webhook-id']+'.'+h['webhook-timestamp']+'.').update(raw).digest('base64');
      assert.equal(raw.toString(),JSON.stringify(event));assert.equal(h['webhook-signature'],expected);assert.equal(h['content-length'],String(raw.length));assert.equal(h['webhook-id'],event.eventId);assert.equal(h['x-mcp-subscription-id'],'sub_fixture');
      h['webhook-signature']='<redacted:verified>';capture={headers:h,body_utf8:raw.toString(),body_sha256:digest(raw),tls_version:req.socket.getProtocol()};res.writeHead(200,{'Content-Type':'application/json'});res.end('{}');
    });
  });
  try {
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    const cfg=path.join(tmp,'fixture.json');fs.writeFileSync(cfg,JSON.stringify({source,sourceHash:digest(fs.readFileSync(source)),signedAt,secret,event,url:`https://127.0.0.1:${server.address().port}/events`}),{mode:0o600});
    const child=spawn(process.execPath,[process.argv[1],'child',cfg],{env:{...process.env,NODE_EXTRA_CA_CERTS:cert},stdio:['ignore','pipe','pipe']});let stdout='',stderr='';child.stdout.on('data',x=>stdout+=x);child.stderr.on('data',x=>stderr+=x);
    const code=await new Promise(resolve=>child.on('exit',resolve));assert.equal(code,0,stderr);
    const evidence={result:'PASS',captured_at:new Date().toISOString(),scope:'selected reviewed Upstash function bodies, actual Node fetch over local TLS; helpers supplied by fixture',public_source_sha256:digest(fs.readFileSync(source)),functions:JSON.parse(stdout),capture,response:{status:200,origin:'local_fixture'},live_model_visibility_proven:false,production_callbacks:0};
    fs.writeFileSync(output,JSON.stringify(evidence,null,2)+'\n');console.log(JSON.stringify({result:'PASS',headers:capture.headers,body_sha256:capture.body_sha256,live_model_visibility_proven:false}));
  } finally {await new Promise(resolve=>server.close(resolve));fs.rmSync(tmp,{recursive:true,force:true});}
}
