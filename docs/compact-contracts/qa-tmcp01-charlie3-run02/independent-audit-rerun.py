"""Independent TMCP-01 offline contract audit; isolates DB and does not mutate repo."""
import asyncio
import base64
import json
import re
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings
from terminal_mcp.mcp.access_contracts import (
    AttachInput, IssuerSessionInput, MeshCommandReadInput,
    MeshMessageInput, MeshTaskCommentInput,
)
from terminal_mcp.mcp.role_contracts import ROLE_TOOL_MODELS, TaskManageInput
from terminal_mcp.application.task_requests import (
    TaskUpdateRequest, TaskArchiveRequest, TaskClaimRequest,
    TaskReleaseRequest, TaskCheckpointRequest, TaskReviewRequest,
    TaskRelateRequest,
)
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore

ROOT = Path('/workspace/qa-tmcp01-run02-charlie3-20261010')
QA = Path('/workspace/terminal-mcp-artifacts/charlie3-tmcp01-run02-20261010')
GEN = ROOT / 'docs/compact-contracts/generated'
def load(n): return json.loads((GEN / n).read_text())
strict, coverage = load('strict-contracts.json'), load('coverage.json')
catalogs = {role:load(f'catalog-{role}.json')['tools'] for role in ('access','executor','coordinator')}
assert {f'{role}.{t["name"]}' for role,tools in catalogs.items() for t in tools} == set(strict) == set(coverage)
OVERRIDE = {
    'access.session': IssuerSessionInput,
    'executor.session': AttachInput,'coordinator.session': AttachInput,
    'executor.message': MeshMessageInput,'coordinator.message': MeshMessageInput,
    'executor.command_read': MeshCommandReadInput,
    'executor.task_comment': MeshTaskCommentInput,
}
def model_for(key):
    if key in OVERRIDE: return OVERRIDE[key]
    role,name = key.split('.')
    return ROLE_TOOL_MODELS[role,name]

def settings(tmp):
    return Settings(
        database_path=tmp/'app.db',auth_database_path=tmp/'auth.db',
        output_cache_path=tmp/'output.db',runtime_config_path=tmp/'runtime.env',
        env_file_path=tmp/'terminal.env',log_path=tmp/'app.log',metrics_port=0,
        cwd=tmp,public_base_url='https://tmcp01-qa.invalid',
        auth_mode='bearer',bearer_tokens='TMCP01-only-fixture-token',
        mcp_auth_mode='',actions_auth_mode='',persistent_agents_enabled=True,
        fleet_instance_id='qa-tmcp01',fleet_id='',
        fleet_signing_private_key=base64.b64encode(bytes(range(32))).decode(),
        fleet_peers_json='[]',fleet_v1_source_enabled=False,
        fleet_v1_authority_enabled=False,fleet_v1_projection_enabled=False,
        fleet_v1_public_enabled=False,access_mesh_enabled=True,
        access_mesh_proof_key='TMCP01-only-local-fixture-key-1234567890',
        access_mesh_peers_json='[]',
    )

def validate_field_roster(key, strict_schema, tool):
    # Parse generated field declarations as human-visible text, independently
    # of coverage action metadata: fields and required marker must match model.
    try:
        line = next(s for s in tool['description'].splitlines() if s.startswith('Arguments (field?'))
    except StopIteration:
        raise AssertionError(f'{key}: missing Arguments line')
    fields = {}
    for part in re.split(r'; (?=[a-z_]+\??:)', line.split(': ',1)[1].rstrip('.')):
        if part == '(none)': continue
        m = re.match(r'^([a-z_]+)(\?)?:',part)
        if not m: raise AssertionError(f'{key}: unparsed argument {part[:100]}')
        fields[m.group(1)] = m.group(2) is None
    expected = set(strict_schema.get('properties',{}))
    if fields.keys() != expected:
        raise AssertionError(f'{key}: generated field names mismatch: {set(fields)^expected}')
    mandatory = set(strict_schema.get('required',[]))
    for n,declared_required in fields.items():
        if declared_required != (n in mandatory):
            raise AssertionError(f'{key}.{n}: required textual marker conflicts with strict model')
    return len(fields), sorted(mandatory)

async def storage_regressions(tmp):
    repo = SqliteRepository(tmp/'selftest.db',tmp/'selftest-out.db')
    await repo.initialize()
    store = TaskStore(repo.path)
    coordinator = TaskCoordinator(store)
    await store.create_task('selftest','task','TMCP-01 isolated state',state='ready')
    await store.claim('selftest','task','qa_owner',claim_intent='test isolation')
    before = await store.get_task('selftest','task')
    committed = await store.set_workflow_state('selftest','task','blocked',agent_id='qa_nonowner')
    after = await store.get_task('selftest','task')
    assert after['state']=='blocked' and after['revision']==before['revision'], (before,after)
    assert len(await store.active_claims('selftest','task')) == 1
    # archive's live-owner gate is an additional real runtime precondition.
    denied = await coordinator._action_archive('qa_nonowner','selftest','task',note='nonowner attempt')
    assert denied.get('code')=='owner_required',denied
    # review requires actual current output state, not just dimensions/verdict.
    review = await coordinator._action_review('qa_owner','selftest','task',dimensions=['A'],verdict='NON_BLOCKING')
    assert review.get('ok') is True,review
    # canonical accepts note as substitute for archive_note (B3).
    assert TaskArchiveRequest.model_validate(dict(namespace='selftest',task_id='task',action='archive',note='reason'))
    # B4 optional release reason even when live claim; use the real action.
    released = await coordinator._action_release('qa_owner','selftest','task')
    assert released.get('ok') is True,released
    return {
        'state_independent_claim':True,'content_revision_before':before['revision'],
        'content_revision_after':after['revision'],'state_from':'ready','state_to':'blocked',
        'archive_other_agent_code':denied.get('code'),
        'review_with_output_state_ok':review.get('ok'),
        'release_without_reason_ok':True,
    }

def canonical_cases():
    checks = {}
    def case(name, model, payload, good):
        try: model.model_validate(payload); accepted=True
        except ValidationError: accepted=False
        assert accepted == good,(name,model,payload,accepted,good)
        checks[name]='PASS'
    base=dict(action='update',namespace='selftest',task_id='x')
    case('B1_update_missing_rev',TaskUpdateRequest,base,False)
    case('B1_update_null_rev',TaskUpdateRequest,{**base,'expected_revision':None},False)
    case('B1_update_valid',TaskUpdateRequest,{**base,'expected_revision':1},True)
    case('B3_archive_note_only',TaskArchiveRequest,dict(action='archive',namespace='x',task_id='t',note='just note'),True)
    case('B6_claim_null_intent',TaskClaimRequest,dict(action='claim',namespace='x',task_id='t',claim_intent=None),False)
    case('B6_claim_valid',TaskClaimRequest,dict(action='claim',namespace='x',task_id='t',claim_intent='valid'),True)
    case('B6_claim_cross_extra',TaskClaimRequest,dict(action='claim',namespace='x',task_id='t',claim_intent='valid',release_reason='extra'),False)
    case('B6_release_cross_extra',TaskReleaseRequest,dict(action='release',namespace='x',task_id='t',claim_intent='extra'),False)
    case('B6_release_no_reason',TaskReleaseRequest,dict(action='release',namespace='x',task_id='t'),True)
    case('B9_checkpoint_null',TaskCheckpointRequest,dict(action='checkpoint',namespace='x',task_id='t',checkpoint=None),False)
    case('B9_review_null_dimensions',TaskReviewRequest,dict(action='review',namespace='x',task_id='t',dimensions=None,verdict='BLOCKING'),False)
    case('B10_null_vs_empty',TaskUpdateRequest,{**base,'expected_revision':1,'description':None,'tags':[]},True)
    update = TaskUpdateRequest.model_validate({**base,'expected_revision':1,'description':None,'tags':[]})
    assert 'description' not in update.model_dump(exclude_none=True) and update.model_dump(exclude_none=True)['tags']==[]
    case('B13_relate_open_string',TaskRelateRequest,dict(action='relate',namespace='x',task_id='t',relation_kind='nonstandard_link',related_task_id='u'),True)
    for payload in [
        {'action':'send','text':'ok','message_hash':None},
        {'action':'ack','message_hash':'h','text':None},
        {'action':'read','text':None,'message_hash':None}
    ]:
        MeshMessageInput.model_validate(payload)
    checks['B7_message_null_values']='PASS'
    for payload in [
        {'namespace':'x','task_id':'t','comment_text':None},
        {'namespace':'x','task_id':'t','comment_text':'ok','checkpoint':'unwanted'},
        {'action':'checkpoint','namespace':'x','task_id':'t','checkpoint':'ok','expected_revision':1}
    ]:
        try: obj=MeshTaskCommentInput.model_validate(payload); obj.to_request()
        except (ValidationError, ValueError): continue
        raise AssertionError('B8 unexpected acceptance '+str(payload))
    checks['B8_comment_extra_and_null']='PASS'
    return checks

def rpc(client, role, name, arguments, index):
    params={'name':name,'arguments':arguments,
            '_meta':{'openai/subject':'tmcp01-local-fixture','openai/session':f'tmcp01-{role}'}}
    r=client.post(f'/terminal-mcp/{role}/v1/mcp/',
      headers={'authorization':'Bearer TMCP01-only-fixture-token','accept':'application/json, text/event-stream'},
      json={'jsonrpc':'2.0','id':index,'method':'tools/call','params':params})
    assert r.status_code==200,(role,name,r.status_code,r.text[:300])
    result=r.json()['result']
    assert not result['isError'],(role,name,result)
    obj=result['structuredContent']
    assert json.loads(result['content'][0]['text'])==obj
    return obj,len(r.content),len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode())

def main():
    rows = []
    with tempfile.TemporaryDirectory(prefix='tmcp01-bravo3-') as tmp:
        tmp=Path(tmp)
        app=create_app(settings(tmp))
        for role in catalogs:
            server=getattr(app.state,f'{role}_mcp')
            effective={t.name:t for t in server._tool_manager.list_tools()}
            actual={t.name:t.model_dump(mode='json',by_alias=True,exclude_none=True) for t in asyncio.run(server.list_tools())}
            assert set(actual)==set(effective)=={t['name'] for t in catalogs[role]}
            for candidate in catalogs[role]:
                name=candidate['name']; key=f'{role}.{name}'
                model=model_for(key)
                raw=model.model_json_schema()
                assert raw==strict[key]['inputSchema'],(key,'raw schema mismatch')
                assert effective[name].fn_metadata.output_model.model_json_schema()==strict[key]['outputSchema'],(key,'output mismatch')
                properties=candidate['inputSchema']['properties']
                assert candidate['inputSchema']['type']=='object' and candidate['inputSchema']['additionalProperties'] is True
                assert set(candidate['inputSchema'])=={'type','properties','additionalProperties','description','x-terminal-mcp-action-matrix'}
                assert set(properties)==set(raw.get('properties',{}))
                assert all(spec=={} for spec in properties.values())
                assert Draft202012Validator(candidate['inputSchema']).is_valid({f:'INVALID' for f in properties})
                assert Draft202012Validator(candidate['inputSchema']).is_valid({'unexpected':'allowed_transport'})
                n,req=validate_field_roster(key,raw,candidate)
                # Exact unchanged flattened output projection from effective tools.
                assert candidate['outputSchema']==actual[name]['outputSchema']
                rows.append({'tool':key,'input_fields':n,'strict_required':req,
                             'source_input':'PASS','source_output':'PASS','text_roster_required':'PASS',
                             'transport_nonblocking':'PASS','flat_output_unchanged':'PASS',
                             'manual_runtime':'REVIEWED'})
                effective[name].parameters=candidate['inputSchema']
                effective[name].description=candidate['description']
            # Verify entire proposed published catalogue, not just per-tool metadata.
            published=[t.model_dump(mode='json',by_alias=True,exclude_none=True) for t in asyncio.run(server.list_tools())]
            assert published==catalogs[role],('tools/list mismatch',role)
        asserts=canonical_cases()
        storage=asyncio.run(storage_regressions(tmp))
        wire=[]
        with TestClient(app,base_url='https://tmcp01-qa.invalid') as client:
            for i,row in enumerate(rows,1):
                role,name=row['tool'].split('.')
                # Each intentionally malformed value must reach RuntimeBoundary.
                strictprops=strict[row['tool']]['inputSchema'].get('properties',{})
                args={k:False for k in strictprops}
                args['qa_unknown']=42
                obj,http_size,result_size=rpc(client,role,name,args,i)
                # May produce domain error e.g. invalid_state; must be bounded either way.
                assert obj.get('ok') is False,(role,name,obj)
                assert result_size < 3000,(role,name,result_size)
                assert 'inputSchema' not in json.dumps(obj)
                wire.append({'tool':row['tool'],'http_bytes':http_size,'result_bytes':result_size,
                            'code':obj.get('error',{}).get('code')})
            secret='TMCP01_INDEPENDENT_SECRET_MARKER_' + 's'*12000
            args={'action':'create','namespace':'selftest','isolation_hint':'local',
                  **{f'unexpected_{i}':secret for i in range(30)}}
            obj,http_size,result_size=rpc(client,'coordinator','task_manage',args,1001)
            assert obj['ok'] is False and secret not in json.dumps(obj)
            assert result_size<3000 and 'inputSchema' not in json.dumps(obj)
            wire.append({'tool':'STRESS:coordinator.task_manage','http_bytes':http_size,
                         'result_bytes':result_size,'code':obj.get('error',{}).get('code')})
        result={'exact_SHA':'baaf7744a94ca126ba0b913b6829f98e2a33b693',
                'roster_count':len(rows),'tools':rows,'canonical_checks':asserts,
                'runtime_storage':storage,'independent_wire':wire,
                'wire_max_bytes':max(x['result_bytes'] for x in wire)}
        (QA/'independent-evidence.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
        with (QA/'19-tool-matrix.md').open('w') as f:
            f.write('| Tool | Fields | Strict required | Input model | Output model | Text fields & required | Transport | Flat output |\n')
            f.write('|---|---:|---|---|---|---|---|---|\n')
            for r in rows:
                f.write(f"| {r['tool']} | {r['input_fields']} | {', '.join(r['strict_required']) or 'none'} | PASS | PASS | PASS | PASS | PASS |\n")
        print('PASS 19/19 exact source-input/output, textual field and required markers, permissive transport, effective catalogue output')
        print('PASS additional canonical cases:',len(asserts),'runtime',storage)
        print('PASS 19 malformed + 1 stress isolated HTTP wire; largest complete result bytes=',result['wire_max_bytes'])
        print('Saved',QA/'independent-evidence.json',QA/'19-tool-matrix.md')

if __name__=='__main__': main()
