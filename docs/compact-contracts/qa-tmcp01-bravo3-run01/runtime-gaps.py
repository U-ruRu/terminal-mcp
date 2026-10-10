"""Independent runtime differential: normal published action payloads vs domain gates."""
import asyncio,json,tempfile
from pathlib import Path
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.application.task_requests import TaskUpdateRequest,TaskArchiveRequest,TaskRelateRequest,TaskUnrelateRequest
from terminal_mcp.mcp.role_contracts import TaskManageInput
QA=Path('/workspace/terminal-mcp-artifacts/bravo3-tmcp01-20261010')
GEN=Path('/workspace/qa-tmcp01-bravo3-20261010/docs/compact-contracts/generated')
catalog=json.loads((GEN/'catalog-coordinator.json').read_text())
desc=next(t['description'] for t in catalog['tools'] if t['name']=='task_manage')
async def main():
    with tempfile.TemporaryDirectory(prefix='tmcp01-gaps-') as tmp:
        repo=SqliteRepository(Path(tmp)/'db.sqlite3',Path(tmp)/'out.sqlite3')
        await repo.initialize()
        store=TaskStore(repo.path)
        core=TaskCoordinator(store)
        await store.create_task('demo','src','Example',lane='implementation')
        await store.create_task('demo','dst','Target')
        await store.claim('demo','src','owner',claim_intent='local QA')
        r=(await store.get_task('demo','src'))['revision']
        examples={
          'archive':{'action':'archive','namespace':'demo','task_id':'src','note':'reason'},
          'relate':{'action':'relate','namespace':'demo','task_id':'src','relation_kind':'blocks','related_task_id':'dst'},
          'unrelate':{'action':'unrelate','namespace':'demo','task_id':'src','relation_kind':'blocks','related_task_id':'dst'},
          'update_unsafe':{'action':'update','namespace':'demo','task_id':'src','expected_revision':r,'lane':'review'},
          'update_safe':{'action':'update','namespace':'demo','task_id':'src','expected_revision':r,'title':'Commenter updated title'},
        }
        models={'archive':TaskArchiveRequest,'relate':TaskRelateRequest,'unrelate':TaskUnrelateRequest,
                'update_unsafe':TaskUpdateRequest,'update_safe':TaskUpdateRequest}
        out={}
        for name,payload in examples.items():
            assert TaskManageInput.model_validate(payload)
            assert models[name].model_validate(payload)
            kwargs={k:v for k,v in payload.items() if k not in ('action','namespace','task_id')}
            action='update' if name.startswith('update') else name
            result=await getattr(core,'_action_'+action)('nonowner','demo','src',**kwargs)
            out[name]={'valid_top_level_and_canonical':True,'ok':result.get('ok'),
                       'code':result.get('code'),'error':result.get('error')}
        assert all(out[n]['code']=='owner_required' for n in ('archive','relate','unrelate','update_unsafe')),out
        assert out['update_safe']['ok'] is True,out
        # Separate task proves candidate_ref freeze is a second reachable update gate.
        await store.create_task('demo','done','Complete',state='done',result={'evidence':'done'})
        rev=(await store.get_task('demo','done'))['revision']
        candidate={'action':'update','namespace':'demo','task_id':'done','expected_revision':rev,
                   'candidate_ref':'candidate-2'}
        assert TaskUpdateRequest.model_validate(candidate)
        fres=await core._action_update('nonowner','demo','done',expected_revision=rev,candidate_ref='candidate-2')
        assert fres.get('code')=='candidate_ref_frozen',fres
        out['candidate_ref_after_done']={'valid_top_level_and_canonical':True,'ok':fres.get('ok'),
                                          'code':fres.get('code'),'error':fres.get('error')}
        for wording in ('owner_required','live owner','candidate_ref_frozen','safe participant','content revision unchanged'):
            out['description_contains_'+wording]=wording.lower() in desc.lower()
        assert not out['description_contains_owner_required']
        assert not out['description_contains_live owner']
        assert not out['description_contains_candidate_ref_frozen']
        (QA/'runtime-gaps.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(out,ensure_ascii=False,indent=2))
asyncio.run(main())
