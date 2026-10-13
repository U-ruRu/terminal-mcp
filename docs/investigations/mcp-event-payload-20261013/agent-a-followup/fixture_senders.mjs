/** Test-only loopback TLS adapter; cannot accept production callback destinations. */
import assert from 'node:assert/strict';
import {createHmac} from 'node:crypto';
import fs from 'node:fs';
import https from 'node:https';
import path from 'node:path';
import {loadOfficialSender,digest} from './reference_sender.mjs';
const cfg=JSON.parse(fs.readFileSync(0,'utf8'));
assert.equal(cfg.host,'receiver.tmcp-fixture.invalid');
const DateClass=class extends Date {constructor(...a){super(...(a.length?a:[cfg.signedAt*1000]));}};
const {sender,Webhook,functionSha256}=loadOfficialSender(cfg.sourceRoot,DateClass);
const hookText=fs.readFileSync(path.join(cfg.sourceRoot,'hookdeck/mcp-events-bridge/src/core/sign.ts'),'utf8');
const hookStart=hookText.indexOf('export function signStandardWebhook(');
const hookEnd=hookText.indexOf('\n}',hookStart)+2;
assert.ok(hookStart>=0 && hookEnd>hookStart);
// Preserve reviewed function body; remove only its TypeScript signature.
const hookBody=hookText.slice(hookText.indexOf('{',hookStart),hookEnd);
const hookSign=new Function('Webhook',`return function(secrets,msgId,body,timestamp=new Date())${hookBody}`)(Webhook);
const reclyText=fs.readFileSync(cfg.reclyFile,'utf8');
const reclyPart=reclyText.slice(reclyText.indexOf('function signingKey('),reclyText.indexOf('\nfunction isPublicIp('));
assert.ok(reclyPart.includes('function signedHeaders(') && !reclyPart.includes('postSigned'));
const reclySign=new Function('createHmac','Date',reclyPart+'\nreturn signedHeaders;')(createHmac,DateClass);
const results=[];
for(const event of cfg.events){
  const subscription={id:'sub_fixture',url:`https://${cfg.host}:${cfg.port}/events`,secret:cfg.secret};
  const body=JSON.stringify(event), stamp=new DateClass();
  const reference=new Webhook(cfg.secret).sign(event.eventId,stamp,body);
  assert.equal(hookSign([cfg.secret],event.eventId,body,stamp)['webhook-signature'],reference);
  // signedHeaders calls Date.now(); expose a fixed now without changing global Date.
  DateClass.now=()=>cfg.signedAt*1000;
  assert.equal(reclySign(subscription,event.eventId,body)['webhook-signature'],reference);
  const fetchFixture=(url,options)=>new Promise((resolve,reject)=>{
    const u=new URL(url);assert.equal(u.hostname,cfg.host);assert.equal(Number(u.port),cfg.port);
    const req=https.request(u,{method:options.method,headers:{...options.headers,'Content-Length':Buffer.byteLength(options.body)},
      ca:fs.readFileSync(cfg.ca),rejectUnauthorized:true,servername:cfg.host,agent:false,
      family:4,autoSelectFamily:false,lookup:(_h,_o,cb)=>cb(null,'127.0.0.1',4)},res=>{
      res.resume();res.on('end',()=>resolve({ok:res.statusCode>=200&&res.statusCode<300,status:res.statusCode}));
    });req.on('error',reject);req.end(options.body);
  });
  results.push(await sender(subscription,event,fetchFixture));
}
console.log(JSON.stringify({result:'PASS',count:results.length,results,official_function_sha256:functionSha256,
  hookdeck_function_sha256:digest(hookText.slice(hookStart,hookEnd)),recly_functions_sha256:digest(reclyPart),
  public_signature_comparisons:'Hookdeck and Recly equal official standardwebhooks for each exact body'}));
