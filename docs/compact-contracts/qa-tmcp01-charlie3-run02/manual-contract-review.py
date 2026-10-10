"""Manual source-linked contract adjudication for all nineteen tools and B1-B13/G1-G2."""
import importlib,json,re
from pathlib import Path
from terminal_mcp.mcp import compact_contracts as cc
from terminal_mcp.mcp.roles import _TASK_MANAGE_MODELS
from terminal_mcp.mcp.access_contracts import MeshMessageInput,MeshTaskCommentInput
from terminal_mcp.application.task_requests import TaskUpdateRequest,TaskArchiveRequest,TaskClaimRequest,TaskReleaseRequest
from terminal_mcp.mcp.role_contracts import TaskManageInput
ROOT=Path('/workspace/qa-tmcp01-run02-charlie3-20261010')
OUT=Path('/workspace/terminal-mcp-artifacts/charlie3-tmcp01-run02-20261010')
D=ROOT/'docs/compact-contracts/generated'
catalog={f'{role}.{r["name"]}':r
         for role in ('access','executor','coordinator')
         for r in json.loads((D/f'catalog-{role}.json').read_text())['tools']}
strict=json.loads((D/'strict-contracts.json').read_text())
coverage=json.loads((D/'coverage.json').read_text())
assert len(catalog)==len(strict)==len(coverage)==19
base_parity=json.loads((OUT/'scope-parity-independent.json').read_text())
assert len(base_parity['tools'])==19
audit=json.loads((OUT/'independent-evidence.json').read_text())
assert audit['roster_count']==19 and audit['wire_max_bytes'] <= 3000
runtime=json.loads((OUT/'runtime-gaps.json').read_text())
blank=json.loads((OUT/'whitespace-regressions.json').read_text())
adverse=json.loads((OUT/'adversarial-runtime-evidence.json').read_text())
desc=lambda key:catalog[key]['description']
def need(key,*phrases):
    missing=[x for x in phrases if x not in desc(key)]
    assert not missing,(key,missing)
assert set(cc.action_models('coordinator','task_manage')) == {'create','update','checkpoint','archive','review','relate','unrelate','state','comment'}
assert set(cc.action_models('executor','task_claim'))=={'claim','release'}
assert set(cc.action_models('executor','task_comment'))=={'comment','checkpoint'}
# Per-action policy is reconstructed independently from canonical Pydantic models,
# without trusting generated coverage/action metadata.
policy={}
for role,name in [('coordinator','task_manage'),('executor','task_claim'),('executor','task_comment')]:
    key=f'{role}.{name}';text=desc(key)
    public=set(strict[key]['inputSchema'].get('properties',{}))
    policy[key]={}
    for action,model in cc.action_models(role,name).items():
        allowed=sorted(set(model.model_fields)&public)
        required={field for field in allowed if model.model_fields[field].is_required()}
        raw_action=cc.contract_models(role)[name].model_fields.get('action')
        if raw_action is not None and not raw_action.is_required() and raw_action.default==action:
            required.discard('action')
        expected=f"{action}: allowed fields [{', '.join(allowed)}]; required non-null [{', '.join(sorted(required)) or '(none)'}]; all other named inputs rejected as extra_forbidden"
        assert expected in text,(key,action,expected)
        policy[key][action]={'allowed':allowed,'required_non_null':sorted(required),'forbidden':sorted(public-set(allowed))}
# Every appended runtime annotation must exist in source and be rendered in its tool description.
notes=cc._annotations()
for key,items in notes.items():
    for note in items:
        src=(ROOT/note['source']).read_text()
        assert note['anchor'] in src,(key,note)
        fragment=(f"action={note['action']}: " if note.get('action') else '') + note['text']
        target_keys=([c for c in catalog if c.endswith(key[1:])] if key.startswith('*.') else [key])
        assert target_keys,(key,'no role matches wildcard')
        for target_key in target_keys:
            assert fragment in desc(target_key),(target_key,fragment)
# Seven exact independently-found problem families in Delta3 descriptions.
need('coordinator.task_manage',
     'archive requires either non-null archive_note or non-null note at canonical model level',
     'Runtime selects raw archive_note if truthy, else note',
     'archive_note="   " masks valid note fallback and fails',
     'requires non-whitespace text')
need('executor.task_claim','release_reason is optional even when an existing live claim is released',
     'idempotent unclaimed release bypasses reason validation','supplied release_reason must be non-whitespace',
     'claim requires non-null claim_intent','runtime trims and rejects whitespace-only intent',
     'action=claim:','action=release:')
need('executor.task_state','any admitted agent may set valid workflow state',
     'content revision is unchanged','live claims persist','repeated state assignment emits no event')
need('executor.task_comment','non-whitespace comment_text','checkpoint does not accept expected_revision',
     'rejects checkpoint','rejects comment_text')
need('coordinator.task_manage','comment requires non-null comment_text with at least one non-whitespace character',
    'action=update: With a live claim, nonowner update permits only safe participant fields title, description, next_action, tags',
    'owner_required','archive, relate and unrelate require the live owner',
    'action=update: Non-null update candidate_ref returns candidate_ref_frozen',
    'task is done or has incoming review_of','even for unchanged value','null is omitted',
    'action=state: Admitted nonowners may set workflow state despite a live claim',
    'no content revision CAS check/increment','existing claim unaffected')
assert all(blank[k]['canonical_accepts'] and not blank[k]['runtime_ok'] for k in (
    'archive_note_whitespace','archive_note_whitespace_masks_valid_note',
    'comment_text_whitespace','release_reason_whitespace','claim_intent_whitespace'))
assert blank['release_reason_omitted']['runtime_ok']
assert all(runtime[k]['code']=='owner_required' for k in ('archive','relate','unrelate','update_unsafe'))
assert runtime['update_safe']['ok'] and runtime['candidate_ref_after_done']['code']=='candidate_ref_frozen'
assert adverse['incoming_review_freeze_same_value']['code']=='candidate_ref_frozen'
assert adverse['incoming_review_candidate_null_is_omitted']['ok']
assert adverse['not_claimed_release_whitespace_bypassed']['ok']
assert all(adverse[k]['ok'] for k in ('unclaimed_relate','unclaimed_unrelate','unclaimed_archive_note','repeated_state','state_nonowner_stale_expected_rev','nonowner_safe_title_with_control_flags_and_null_candidate'))
assert adverse['nonowner_cooperative_unsafe']['code']=='owner_required'
assert adverse['state_invariants']['revision_before']==adverse['state_invariants']['revision_after']
assert adverse['state_invariants']['events_before']==adverse['state_invariants']['events_after_repeat']
assert adverse['state_invariants']['claim_preserved']
# Direct canonical examples that originally diverged.
assert TaskManageInput.model_validate({'action':'archive','namespace':'qa','task_id':'t','archive_note':'   ','note':'valid fallback'})
assert TaskArchiveRequest.model_validate({'action':'archive','namespace':'qa','task_id':'t','archive_note':'   ','note':'valid fallback'})
for p in [{'action':'claim','namespace':'qa','task_id':'t','claim_intent':'   '},
          {'action':'release','namespace':'qa','task_id':'t','release_reason':'   '}]:
    (TaskClaimRequest if p['action']=='claim' else TaskReleaseRequest).model_validate(p)
assert MeshTaskCommentInput.model_validate({'comment_text':'   ','namespace':'qa','task_id':'t'})
# 19-row human review: 15 untouched descriptions inherit the completed RUN01
# manual source check; four were independently source-anchored above and tested.
rows=[]
for key,tool in sorted(catalog.items()):
    old=next(t for t in base_parity['tools'] if t['name']==key)
    newdesc=bool(old['only_changed_field'])
    assert (key in {'executor.task_claim','executor.task_state','executor.task_comment','coordinator.task_manage'})==newdesc
    check=next(t for t in audit['tools'] if t['tool']==key)
    assert all(check[x]=='PASS' for x in ('source_input','source_output','text_roster_required','transport_nonblocking','flat_output_unchanged'))
    rows.append({'tool':key,'field_count':check['input_fields'],'strict_required':check['strict_required'],
                 'description_changed':newdesc,'source_input':'PASS','source_output':'PASS',
                 'semantic_contract':'PASS','public_transport':'PASS','public_output':'PASS'})
ids={
'B1':('PASS','update expected_revision non-null and required'),
'B2':('PASS','all 9 canonical action allowlists/required/extras exact'),
'B3':('PASS','archive note precedence/whitespace masked fallback accurately described'),
'B4':('PASS','release_reason optional, supplied nonblank, unclaimed bypass'),
'B5':('PASS','task_state admitted nonowner, CAS revision stable, claims persist'),
'B6':('PASS','claim_intent nonblank, cross-action claim/release exclusions'),
'B7':('PASS','message null-vs-presence and scopes unchanged; source validators untouched'),
'B8':('PASS','comment nonblank, checkpoint forbidden-extra/nullable'),
'B9':('PASS','action-specific nonnull requirements unchanged, source scope stable'),
'B10':('PASS','update None omitted, empty arrays replace; prior independently verified'),
'B11':('PASS_OFFLINE_LIVE_NOT_TESTED','permissive 19 transport projections and 20 bounded HTTP errors'),
'B12':('PASS','task_comment checkpoint expected_revision forbidden'),
'B13':('PASS','open relation_kind plus runtime review_of; unchanged'),
'G1':('PASS','owner gates vs safe participant fields, unclaimed path tested'),
'G2':('PASS','candidate_ref frozen done/incoming review_of incl same value, null omitted')
}
out={'candidate':'baaf7744a94ca126ba0b913b6829f98e2a33b693',
      'verdict':'NON_BLOCKING_OFFLINE','b_and_g':{k:{'verdict':v,'basis':basis} for k,(v,basis) in ids.items()},
      'action_policies':policy,'all_19_tools':rows}
(OUT/'b-g-manual-adjudication.json').write_text(json.dumps(out,indent=2,ensure_ascii=False)+'\n')
with (OUT/'manual-19-tool-matrix.md').open('w') as f:
 f.write('| Tool | Fields | Strict required | Description delta | Source validation | Description / runtime verdict |\n|---|---:|---|---|---|---|\n')
 for row in rows:
  f.write(f"| `{row['tool']}` | {row['field_count']} | {', '.join(row['strict_required']) or 'none'} | {'Repaired' if row['description_changed'] else 'Unchanged'} | PASS | PASS offline |\n")
print('PASS all 19 manual source/input/output/description rows; only 4 repaired descriptions')
print('PASS 9 + 2 + 2 canonical action whitelist/required/extra policies')
print('PASS 13 B IDs + G1/G2, source anchors/action scopes; runtime adversarial + whitespace cases')
print('Verdict NON_BLOCKING_OFFLINE; live ChatGPT A/B NOT TESTED')
