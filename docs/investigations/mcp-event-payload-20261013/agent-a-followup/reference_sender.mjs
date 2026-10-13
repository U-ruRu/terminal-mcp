/** One approved synthetic event through the reviewed OpenAI sender; no subscriptions or retries.
 * Input: stdin JSON {subscription:{id,url,secret},event:{eventId,name,timestamp,data,cursor}}.
 * --sources points to the reviewed source cache; --capture writes a sanitized 0600 receipt.
 * Never pass credentials in argv/env. The caller owns authorization and blind-test scope.
 */
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {lookup} from 'node:dns/promises';
import {execFileSync} from 'node:child_process';
import fs from 'node:fs';
import https from 'node:https';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
export const DOC_SHA256 = '68c534c9d1bd3f9322b31b515f10d56d69cbcefb6c6be1e314c116b2a68e0632';
export const digest = value => createHash('sha256').update(value).digest('hex');

export function loadOfficialSender(sourceRoot, DateClass = Date) {
  const raw = fs.readFileSync(path.join(sourceRoot, 'openai-mcp-events.md'));
  assert.equal(digest(raw), DOC_SHA256, 'unreviewed_doc_snapshot');
  const source = raw.toString('utf8').match(/export async function sendEvent\(subscription, event, webhookFetch\) \{[\s\S]*?\n\}/)?.[0];
  assert.ok(source, 'official_function_missing');
  const require = createRequire(path.join(sourceRoot, 'js', 'package.json'));
  assert.equal(require('standardwebhooks/package.json').version, '1.1.1', 'unreviewed_library');
  const {Webhook} = require('standardwebhooks');
  // Only this hash-pinned, previously reviewed function executes; no page/repository bootstrap.
  const sender = new Function('Webhook', 'Date', source.replace(/^export /, '') + '\nreturn sendEvent;')(Webhook, DateClass);
  return {sender, Webhook, functionSha256: digest(source)};
}

export function validateInput(input) {
  assert.ok(input && typeof input === 'object');
  const {subscription:s, event:e} = input;
  assert.ok(s && e && typeof s.id === 'string' && s.id.length > 0 && s.id.length < 256);
  assert.ok(typeof s.url === 'string' && s.url.length <= 2048);
  const u = new URL(s.url);
  assert.ok(u.protocol === 'https:' && !u.username && !u.password && !u.hash, 'invalid_callback');
  assert.ok(typeof s.secret === 'string' && /^whsec_[A-Za-z0-9+/]+={0,2}$/.test(s.secret));
  const key = Buffer.from(s.secret.slice(6), 'base64');
  assert.ok(key.length >= 24 && key.length <= 64);
  assert.equal(key.toString('base64').replace(/=+$/, ''), s.secret.slice(6).replace(/=+$/, ''));
  assert.deepEqual(Object.keys(e).sort(), ['cursor','data','eventId','name','timestamp']);
  assert.ok(typeof e.eventId === 'string' && /^[A-Za-z0-9_:.-]{1,200}$/.test(e.eventId));
  assert.ok(typeof e.name === 'string' && e.name.length <= 200);
  assert.ok(typeof e.timestamp === 'string' && /T.*(?:Z|[+-]\d{2}:\d{2})$/.test(e.timestamp) && !Number.isNaN(Date.parse(e.timestamp)));
  assert.equal(e.cursor,null,'nonreplayable_fixture_requires_null_cursor');
  assert.ok(e.data && typeof e.data === 'object' && !Array.isArray(e.data));
  assert.ok(Buffer.byteLength(JSON.stringify(e)) <= 262144);
  return input;
}

export function redactResponse(bytes, subscription) {
  let text = bytes.toString('utf8');
  const u = new URL(subscription.url);
  const sensitive = [subscription.url, subscription.secret, subscription.secret.slice(6),
    ...u.pathname.split('/').filter(x => x.length >= 8), ...u.searchParams.values()];
  for (const value of sensitive.filter(x => x.length >= 8).sort((a,b)=>b.length-a.length)) {
    text = text.split(value).join('<redacted>');
  }
  text = text.replace(/whsec_[A-Za-z0-9+/=]+/g, '<secret:redacted>')
    .replace(/Bearer\s+[^\s"<>]+/gi, 'Bearer <redacted>')
    .replace(/v1,[A-Za-z0-9+/=]+/g, '<signature:redacted>');
  // Keep JSON error details while dropping credential-valued response properties.
  try {
    const walk = value => {
      if (Array.isArray(value)) return value.map(walk);
      if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k,v]) =>
        [k, /token|secret|authorization|cookie|signature|callback|url/i.test(k) ? '<redacted>' : walk(v)]));
      return value;
    };
    const parsed = JSON.parse(text), safe = walk(parsed);
    if (JSON.stringify(safe) !== JSON.stringify(parsed)) text = JSON.stringify(safe);
  } catch {}
  return {body_utf8:text, body_bytes:bytes.length, original_body_sha256:digest(bytes),
    redactions_applied:text !== bytes.toString('utf8')};
}

export async function sendApprovedEvent(input, sourceRoot, onPhase = () => {}) {
  validateInput(input);
  const {subscription, event} = input;
  let capture;
  const fetchPinned = async (url, options) => {
    assert.equal(url, subscription.url); assert.equal(options.method, 'POST');
    assert.equal(options.redirect, 'error');
    const parsed = new URL(url), host = parsed.hostname.replace(/^\[|\]$/g, '');
    const addresses = await lookup(host, {all:true, verbatim:true});
    assert.ok(addresses.length, 'empty_dns');
    // Reuse stdlib IP classification, not a partial handwritten IPv6 block list.
    const check = JSON.parse(execFileSync('python3', ['-I','-c',
      'import sys,json,ipaddress; a=json.load(sys.stdin); print(json.dumps({"ok":bool(a) and all(ipaddress.ip_address(x["address"]).is_global for x in a)}))'],
      {input:JSON.stringify(addresses), encoding:'utf8', timeout:5000, maxBuffer:1024}));
    assert.equal(check.ok, true, 'unsafe_address');
    const pinned = addresses[0], body = Buffer.from(options.body, 'utf8');
    const headers = {...options.headers, 'Content-Length':String(body.length)};
    const started = Date.now();
    onPhase('request_started');
    const response = await new Promise((resolve, reject) => {
      const req = https.request(parsed, {method:'POST', headers, agent:false,
        servername:host, family:pinned.family, autoSelectFamily:false,
        rejectUnauthorized:true, signal:options.signal,
        lookup:(_hostname,_options,cb)=>cb(null,pinned.address,pinned.family)}, res => {
        const chunks=[]; let count=0;
        res.on('data', chunk=>{count+=chunk.length; if(count>65536) res.destroy(new Error('response_too_large')); else chunks.push(chunk);});
        res.on('error',reject);
        res.on('end',()=>resolve({status:res.statusCode,headers:res.headers,body:Buffer.concat(chunks)}));
      });
      req.on('error',reject); req.end(body);
    });
    onPhase('response_received', response.status);
    const safeHeaders = {...headers}; delete safeHeaders['webhook-signature'];
    safeHeaders['webhook-signature']='<redacted>';
    const allowedResponse = ['content-type','content-length','date','x-request-id','x-openai-request-id','cf-ray','traceparent'];
    capture = {capture_kind:'real_https_callback',captured_at:new Date().toISOString(),
      request:{method:'POST',callback_host:host,callback_path_sha256:digest(parsed.pathname+parsed.search),
        headers:safeHeaders,body_utf8:body.toString('utf8'),body_bytes:body.length,body_sha256:digest(body)},
      response:{status:response.status,headers:Object.fromEntries(Object.entries(response.headers).filter(([k])=>allowedResponse.includes(k))),
        ...redactResponse(response.body,subscription)},elapsed_ms:Date.now()-started};
    // A 3xx is recorded but never followed. The sender reports accepted=false.
    return {ok:response.status>=200 && response.status<300,status:response.status};
  };
  const {sender,functionSha256} = loadOfficialSender(sourceRoot);
  const result = await sender(subscription,event,fetchPinned);
  return {...result, sender:'OpenAI documented sendEvent / standardwebhooks 1.1.1',
    doc_sha256:DOC_SHA256,function_sha256:functionSha256,live_model_visibility_proven:false,capture};
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  let phase='not_attempted', fd=null, eventId=null, responseStatus=null;
  const record = value => {
    fs.ftruncateSync(fd,0); fs.writeSync(fd,JSON.stringify(value,null,2)+'\n',0,'utf8'); fs.fsyncSync(fd);
  };
  try {
    const args=process.argv.slice(2), arg=name=>args[args.indexOf(name)+1];
    assert.ok(args.includes('--sources') && args.includes('--capture'));
    const bytes=fs.readFileSync(0); assert.ok(bytes.length<=300000);
    const input=validateInput(JSON.parse(bytes.toString('utf8'))); eventId=input.event.eventId;
    // Exclusive preflight reservation prevents accidental re-send to an existing capture path.
    fd=fs.openSync(arg('--capture'),'wx',0o600);
    record({state:'attempt_reserved',eventId,auto_retry:false});
    const receipt=await sendApprovedEvent(input,arg('--sources'),(next,status)=>{
      phase=next; responseStatus=status??null;
      record({state:next,eventId,status:responseStatus,auto_retry:false});
    });
    record(receipt); fs.closeSync(fd); fd=null;
    console.log(JSON.stringify({accepted:receipt.accepted,status:receipt.status,eventId,
      capture_file:arg('--capture'),body_sha256:receipt.capture.request.body_sha256,
      response_body_sha256:receipt.capture.response.original_body_sha256,response_bytes:receipt.capture.response.body_bytes,
      request_id:receipt.capture.response.headers['x-request-id']??null,live_model_visibility_proven:false}));
  } catch {
    const outcome=phase==='not_attempted'?'not_attempted':phase==='response_received'?'http_response_received_receipt_incomplete':'delivery_outcome_unknown';
    if(fd!==null){try{record({state:'failed',eventId,outcome,status:responseStatus,auto_retry:false});fs.closeSync(fd);}catch{}}
    // Exceptions may contain credentials: emit fixed categories only, never error text.
    console.error(JSON.stringify({ok:false,error:'reference_send_failed',outcome,status:responseStatus,auto_retry:false}));
    process.exitCode=1;
  }
}
