"""Independent adversarial positive/negative runtime acceptance probes on disposable SQLite."""
import asyncio,json,sqlite3,tempfile
from pathlib import Path
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore
from terminal_mcp.application.task_requests import TaskUpdateRequest, TaskArchiveRequest, TaskRelateRequest, TaskReleaseRequest
from terminal_mcp.mcp.role_contracts import TaskManageInput
OUT=Path('/workspace/terminal-mcp-artifacts/charlie3-tmcp01-run02-20261010')

async def main():
 with tempfile.TemporaryDirectory(prefix='tmcp01-run02-qa-') as td:
  tmp=Path(td);repo=SqliteRepository(tmp/'db.sqlite',tmp/'output.sqlite'); await repo.initialize()
  store=TaskStore(repo.path); c=TaskCoordinator(store)
  cases={}
  def log(n,result):
   cases[n]={'ok':result.get('ok'),'code':result.get('code'),'error':result.get('error')}
   return result
  await store.create_task('qa','owner-task','Owned safe-field probe',lane='implementation')
  await store.claim('qa','owner-task','owner',claim_intent='owner')
  rev=(await store.get_task('qa','owner-task'))['revision']
  # Flags are legitimate control args; safe-field restriction applies to effective content mutations.
  payload=dict(action='update',namespace='qa',task_id='owner-task',expected_revision=rev,title='Nonowner title',force=True,force_reason='QA trace',candidate_ref=None)
  assert TaskManageInput.model_validate(payload)
  assert TaskUpdateRequest.model_validate(payload)
  safe=log('nonowner_safe_title_with_control_flags_and_null_candidate',await c._action_update('other','qa','owner-task',
          expected_revision=rev,title='Nonowner title',force=True,force_reason='QA trace',candidate_ref=None))
  assert safe.get('ok') is True,safe
  rev2=(await store.get_task('qa','owner-task'))['revision']
  assert rev2>rev
  unsafe=log('nonowner_cooperative_unsafe',await c._action_update('other','qa','owner-task',expected_revision=rev2,cooperative=True))
  assert unsafe.get('code')=='owner_required',unsafe
  stale=log('nonowner_stale_revision',await c._action_update('other','qa','owner-task',expected_revision=rev,title='Stale'))
  assert stale.get('ok') is False,stale
  before=await store.get_task('qa','owner-task')
  assert before['revision']==rev2
  changed=log('state_nonowner_stale_expected_rev',await c._action_state('other','qa','owner-task',state='deferred',expected_revision=999999))
  assert changed.get('ok') is True,changed
  after=await store.get_task('qa','owner-task')
  assert after['state']=='deferred' and after['revision']==before['revision']
  with sqlite3.connect(store.path) as db:
   count1=db.execute("SELECT COUNT(*) FROM work_events WHERE namespace='qa' AND task_id='owner-task' AND event_type='updated'").fetchone()[0]
  again=log('repeated_state',await c._action_state('other','qa','owner-task',state='deferred',expected_revision=0))
  assert again.get('ok') is True,again
  with sqlite3.connect(store.path) as db:
   count2=db.execute("SELECT COUNT(*) FROM work_events WHERE namespace='qa' AND task_id='owner-task' AND event_type='updated'").fetchone()[0]
  assert count1==count2,(count1,count2)
  assert len(await store.active_claims('qa','owner-task'))==1
  cases['state_invariants']={'revision_before':before['revision'],'revision_after':after['revision'],
                             'events_before':count1,'events_after_repeat':count2,'claim_preserved':True}

  # No live owner gate when task unclaimed; all three operations should work.
  await store.create_task('qa','unclaimed','Unclaimed',lane='implementation')
  await store.create_task('qa','target','Relation target')
  rel=log('unclaimed_relate',await c._action_relate('stranger','qa','unclaimed',relation_kind='blocks',related_task_id='target'))
  assert rel.get('ok') is True,rel
  unrel=log('unclaimed_unrelate',await c._action_unrelate('stranger','qa','unclaimed',relation_kind='blocks',related_task_id='target'))
  assert unrel.get('ok') is True,unrel
  arch=log('unclaimed_archive_note',await c._action_archive('stranger','qa','unclaimed',note='reason'))
  assert arch.get('ok') is True,arch

  # Valid model; server freeze with unchanged candidate_ref when incoming review_of exists.
  await store.create_task('qa','parent','Parent',candidate_ref='candidate-alpha')
  await store.create_task('qa','review','Review',lane='review',candidate_ref='candidate-alpha')
  ref=log('review_of_relation',await c._action_relate('reviewer','qa','review',
      relation_kind='review_of',related_task_id='parent'))
  assert ref.get('ok') is True,ref
  pr=(await store.get_task('qa','parent'))['revision']
  frozen=log('incoming_review_freeze_same_value',await c._action_update('stranger','qa','parent',
             expected_revision=pr,candidate_ref='candidate-alpha'))
  assert frozen.get('code')=='candidate_ref_frozen',frozen
  null=log('incoming_review_candidate_null_is_omitted',await c._action_update('stranger','qa','parent',
             expected_revision=pr,candidate_ref=None,title='Safe title'))
  assert null.get('ok') is True,null

  # Idempotent release without own claim bypasses clean_reason on whitespace string.
  await store.create_task('qa','idle','Idle')
  TaskReleaseRequest.model_validate(dict(action='release',namespace='qa',task_id='idle',release_reason='   '))
  res=log('not_claimed_release_whitespace_bypassed',await c._action_release('stranger','qa','idle',release_reason='   '))
  assert res.get('ok') is True,res

  (OUT/'adversarial-runtime-evidence.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2)+'\n')
  print('PASS adversarial runtime cases:',len(cases))
  print(json.dumps(cases,ensure_ascii=False,indent=2))
asyncio.run(main())
