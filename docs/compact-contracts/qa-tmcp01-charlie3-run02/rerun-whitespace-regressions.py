"""Normal canonical-valid nonblank-vs-nonnull boundary checks; private temporary DB only."""
import asyncio,json,tempfile
from pathlib import Path
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore
from terminal_mcp.application.task_requests import TaskArchiveRequest,TaskReleaseRequest,TaskCommentRequest,TaskClaimRequest
Q=Path('/workspace/terminal-mcp-artifacts/charlie3-tmcp01-run02-20261010')
async def main():
 with tempfile.TemporaryDirectory(prefix='tmcp01-null-') as tmp:
  p=Path(tmp); repo=SqliteRepository(p/'db.sqlite3',p/'out.sqlite3');await repo.initialize()
  store=TaskStore(repo.path); core=TaskCoordinator(store)
  await store.create_task('qa','t','Whitespace QA')
  await store.claim('qa','t','owner',claim_intent='intent')
  rows={}
  for name,Model,payload,call in [
   ('archive_note_whitespace',TaskArchiveRequest,dict(action='archive',namespace='qa',task_id='t',note='   '),core._action_archive),
   ('comment_text_whitespace',TaskCommentRequest,dict(action='comment',namespace='qa',task_id='t',comment_text='   '),core._action_comment),
   ('release_reason_whitespace',TaskReleaseRequest,dict(action='release',namespace='qa',task_id='t',release_reason='   '),core._action_release),
  ]:
   Model.model_validate(payload)
   result=await call('owner','qa','t',**{k:v for k,v in payload.items() if k not in ('action','namespace','task_id')})
   rows[name]={'canonical_accepts':True,'runtime_ok':result.get('ok'), 'runtime_code':result.get('code'),
               'runtime_error':result.get('error')}
   assert result.get('ok') is False,(name,result)
  # Archive: two notes, one meaningful. A whitespace-first note masks the valid fallback.
  both=dict(action='archive',namespace='qa',task_id='t',archive_note='   ',note='valid fallback')
  TaskArchiveRequest.model_validate(both)
  attempt=await core._action_archive('owner','qa','t',archive_note='   ',note='valid fallback')
  assert attempt.get('ok') is False,attempt
  rows['archive_note_whitespace_masks_valid_note']={
    'canonical_accepts':True,'runtime_ok':attempt.get('ok'),'runtime_error':attempt.get('error')}
  await store.create_task('qa','u','Second QA task')
  TaskClaimRequest.model_validate(dict(action='claim',namespace='qa',task_id='u',claim_intent='   '))
  claimed=await core._action_claim('owner','qa','u',claim_intent='   ')
  assert claimed.get('ok') is False,claimed
  rows['claim_intent_whitespace']={'canonical_accepts':True,'runtime_ok':claimed.get('ok'),
    'runtime_code':claimed.get('code'),'runtime_error':claimed.get('error')}
  valid=await core._action_release('owner','qa','t')
  assert valid.get('ok') is True,valid
  rows['release_reason_omitted']={'runtime_ok':True}
  print(json.dumps(rows,ensure_ascii=False,indent=2))
  (Q/'whitespace-regressions.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
asyncio.run(main())
