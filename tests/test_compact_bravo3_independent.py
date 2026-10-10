"""Bravo-3 independent acceptance: raw source model + catalog + isolated MCP wire."""
from __future__ import annotations

import asyncio
import json
import pathlib
import tempfile
from collections import Counter

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings
from terminal_mcp.mcp.access_contracts import (
    IssuerSessionInput, AttachInput, MeshCommandReadInput,
    MeshMessageInput, MeshTaskCommentInput,
)
from terminal_mcp.mcp.role_contracts import ROLE_TOOL_MODELS, TaskManageInput
from terminal_mcp.application.task_requests import (
    TaskUpdateRequest, TaskArchiveRequest, TaskReviewRequest,
    TaskCreateRequest, TaskCheckpointRequest, TaskStateRequest,
)
from terminal_mcp.mcp.role_contracts import RuntimeBoundary, validate_boundary

ROOT = pathlib.Path(__file__).resolve().parents[1]
GEN = ROOT / "docs/compact-contracts/generated"
SHA = "d1caea24e2d8c2dff650568fac616bcee0efb99b"
ROLES = ("access", "executor", "coordinator")

def load(name):
    return json.loads((GEN/name).read_text())

def model_of(role, name):
    if role == "access":
        assert name == "session"
        return IssuerSessionInput
    override = {
        "session": AttachInput,
        "message": MeshMessageInput,
        "command_read": MeshCommandReadInput,
        "task_comment": MeshTaskCommentInput,
    }
    if name in override and (role != "coordinator" or name not in {"command_read", "task_comment"}):
        return override[name]
    return ROLE_TOOL_MODELS[role,name]

CATALOGS = {role:load(f"catalog-{role}.json")["tools"] for role in ROLES}
SIDECAR = load("strict-contracts.json")

# Evaluated by hand against access_contracts.py, role_contracts.py,
# task_requests.py, roles.py, access.py and the generated descriptions.
CASES = {
    "access.session": ({"action":"start"}, {"action":"other"}),
    "executor.session": ({"session_number":"0010"}, {"session_number":"1"}),
    "executor.task_list": ({}, {"limit":0}),
    "executor.command_run": ({"command":"printf qa"}, {"command":12}),
    "executor.command_read": ({}, {"limit":101}),
    "executor.command_cancel": ({"cmd_hash":"h"}, {"cmd_hash":""}),
    "executor.command_recovery": ({"command":"printf qa"}, {"command":""}),
    "executor.task_claim": ({"action":"release","namespace":"qa","task_id":"t"}, {"action":"claim","namespace":"qa","task_id":"t","claim_intent":1}),
    "executor.task_state": ({"namespace":"qa","task_id":"t","state":"qa"}, {"namespace":"qa","task_id":"t","state":"bad"}),
    "executor.task_comment": ({"namespace":"qa","task_id":"t","comment_text":"ok"}, {"action":"checkpoint","namespace":"qa","task_id":"t","checkpoint":None}),
    "executor.message": ({"action":"read"}, {"action":"ack","message_hash":None}),
    "coordinator.session": ({"session_number":"0010"}, {"session_number":"x"}),
    "coordinator.task_get": ({"namespace":"qa","task_id":"t"}, {"namespace":"qa","task_id":"t","detail":"unknown"}),
    "coordinator.task_list": ({}, {"tags":[""]}),
    "coordinator.task_manage": ({"action":"create","namespace":"qa","isolation_hint":"isolated"}, {"action":"update","namespace":"qa","task_id":"t","title":"change"}),
    "coordinator.task_graph": ({"namespace":"qa","task_id":"t"}, {"namespace":"qa","task_id":"t","direction":"sideways"}),
    "coordinator.agent_observe": ({}, {"unexpected":True}),
    "coordinator.message": ({"action":"read"}, {"action":"ack","message_hash":None}),
    "coordinator.health": ({}, {"extended":"yes"}),
}
assert len(CASES) == 19

@pytest.mark.parametrize("key", list(CASES))
def test_manual_roster_sources_and_minimal_boundaries(key):
    role,name=key.split(".",1)
    item=next(t for t in CATALOGS[role] if t["name"]==name)
    model=model_of(role,name)
    strict=model.model_json_schema()
    assert strict == SIDECAR[key]["inputSchema"], key
    assert set(item["inputSchema"]["properties"])==set(strict.get("properties",{})),key
    assert not item["inputSchema"].get("required",[]),key
    assert item["inputSchema"].get("additionalProperties") is True,key
    assert all(not spec for spec in item["inputSchema"]["properties"].values()),key
    assert Draft202012Validator(item["inputSchema"]).is_valid(CASES[key][1]),key
    assert all(f in item["description"] for f in strict.get("properties",{})),key
    Draft202012Validator.check_schema(item["inputSchema"])
    valid,invalid=CASES[key]
    assert not list(Draft202012Validator(item["inputSchema"]).iter_errors(valid)),key
    assert model.model_validate(valid)
    assert not list(Draft202012Validator(strict).iter_errors(valid)),key
    if key == "coordinator.task_manage":
        assert model.model_validate(invalid)  # top-level permissive, canonical action enforces
    else:
        with pytest.raises(ValidationError):
            model.model_validate(invalid)

def test_role_publication_against_actual_isolated_app(tmp_path):
    app=create_app(make_settings(tmp_path))
    for role in ROLES:
        server=getattr(app.state,f"{role}_mcp")
        actual=[t.model_dump(mode="json",by_alias=True,exclude_none=True)
                for t in asyncio.run(server.list_tools())]
        baseline=load(f"before-{role}.json")["tools"]
        assert actual==baseline
        assert {t["name"] for t in actual}=={t["name"] for t in CATALOGS[role]}
        for tool in server._tool_manager.list_tools():
            assert tool.fn_metadata.output_model.model_json_schema() == SIDECAR[f"{role}.{tool.name}"]["outputSchema"]

def make_settings(path):
    import base64
    return Settings(
        database_path=path/"db.sqlite3",auth_database_path=path/"auth.sqlite3",
        output_cache_path=path/"output.sqlite3",runtime_config_path=path/"runtime.env",
        env_file_path=path/"terminal.env",log_path=path/"terminal.log",
        metrics_port=0,cwd=path,public_base_url="https://qa-bravo3.example",
        auth_mode="bearer",bearer_tokens="bravo3-isolated-token",
        mcp_auth_mode="",actions_auth_mode="",persistent_agents_enabled=True,
        fleet_instance_id="bravo3",fleet_id="",
        fleet_signing_private_key=base64.b64encode(bytes(range(32))).decode(),
        fleet_peers_json="[]",fleet_v1_source_enabled=False,
        fleet_v1_authority_enabled=False,fleet_v1_projection_enabled=False,
        fleet_v1_public_enabled=False,access_mesh_enabled=True,
        access_mesh_proof_key="qa-bravo3-fixture-not-production",
        access_mesh_peers_json="[]",
    )

def rpc(client,role,name,args,index):
    params={"name":name,"arguments":args,
            "_meta":{"openai/subject":"bravo3-subject","openai/session":f"qa-bravo3-{role}"}}
    resp=client.post(f"/terminal-mcp/{role}/v1/mcp/",
        headers={"authorization":"Bearer bravo3-isolated-token",
                 "accept":"application/json, text/event-stream"},
        json={"jsonrpc":"2.0","id":index,"method":"tools/call","params":params})
    assert resp.status_code==200,(role,name,resp.status_code,resp.text[:300])
    raw=resp.json()
    assert "result" in raw,raw
    result=raw["result"]
    assert not result["isError"],(role,name,result)
    payload=result["structuredContent"]
    assert json.loads(result["content"][0]["text"])==payload
    return payload,len(resp.content),len(json.dumps(result,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode())

def apply_future_catalogues(app):
    """Swap only discovery schemas/descriptions in an isolated process, no deploy."""
    for role in ROLES:
        server = getattr(app.state, f"{role}_mcp")
        intended = {item["name"]: item for item in CATALOGS[role]}
        for tool in server._tool_manager.list_tools():
            tool.parameters = intended[tool.name]["inputSchema"]
            tool.description = intended[tool.name]["description"]
        actual = [
            t.model_dump(mode="json", by_alias=True, exclude_none=True)
            for t in asyncio.run(server.list_tools())
        ]
        assert actual == CATALOGS[role], role


def test_isolated_mcp_wire_19_invalid_and_many_valid(tmp_path):
    app=create_app(make_settings(tmp_path))
    apply_future_catalogues(app)
    error_rows=[]
    with TestClient(app,base_url="https://qa-bravo3.example") as client:
        for i,(key,(valid,invalid)) in enumerate(CASES.items(),1):
            role,name=key.split(".",1)
            err,bytes_http,bytes_result=rpc(client,role,name,invalid,i*2)
            assert err.get("ok") is False, (key,err)
            assert err["error"]["code"]==("invalid_state" if key == "executor.task_state" else "input_validation_failed"),(key,err)
            assert "inputSchema" not in json.dumps(err),(key,err)
            assert bytes_result < 3000,(key,bytes_result)
            error_rows.append({"tool":key,"wire_bytes":bytes_http,"result_bytes":bytes_result,
                               "structured_bytes":len(json.dumps(err,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()),
                               "error_bytes":len(json.dumps(err["error"],sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()),
                               "path":err["error"].get("path")})
            good,_,_=rpc(client,role,name,valid,i*2+1)
            assert good.get("error",{}).get("code") != "input_validation_failed",(key,good)
        # Stress bounded errors: long secret values, > 5 issue cap, nested path.
        leak="QA_BRAVO3_DO_NOT_DISCLOSE_opaque_secret_"+"x"*8192
        stress=[("coordinator","task_manage",{"action":"create","namespace":"qa","isolation_hint":"isolated",**{f"bad_{j}":leak for j in range(80)}}),
                ("executor","command_run",{"command":leak*5}),
                ("executor","message",{"action":"send","message_hash":leak,"text":leak}),
                ("coordinator","task_list",{"tags":[None]*300})]
        for j,(role,name,args) in enumerate(stress,200):
            obj,wire,size=rpc(client,role,name,args,j)
            assert obj["error"]["code"]=="input_validation_failed",(role,name,obj)
            assert leak not in json.dumps(obj)
            assert "inputSchema" not in json.dumps(obj)
            error_rows.append({"tool":f"STRESS:{role}.{name}","wire_bytes":wire,"result_bytes":size,
                               "structured_bytes":len(json.dumps(obj,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()),
                               "error_bytes":len(json.dumps(obj["error"],sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()),
                               "path":obj["error"].get("path")})
    dest=ROOT/"docs/compact-contracts/qa-echo3-wire-metrics.json"
    dest.write_text(json.dumps({"qa_parent":SHA,"errors":error_rows},indent=2)+"\n",encoding="utf-8")
    assert len(error_rows)==23

@pytest.mark.parametrize("key,payload,canonical,source_path",[
  ("update_missing_expected_revision",{"action":"update","namespace":"qa","task_id":"t","title":"x"},TaskUpdateRequest,"expected_revision"),
  ("review_null_dimensions",{"action":"review","namespace":"qa","task_id":"t","dimensions":None,"verdict":"NON_BLOCKING"},TaskReviewRequest,"dimensions"),
  ("review_null_verdict",{"action":"review","namespace":"qa","task_id":"t","dimensions":["A"],"verdict":None},TaskReviewRequest,"verdict"),
  ("checkpoint_null",{"action":"checkpoint","namespace":"qa","task_id":"t","checkpoint":None},TaskCheckpointRequest,"checkpoint"),
  ("state_null",{"action":"state","namespace":"qa","task_id":"t","state":None},TaskStateRequest,"state"),
  ("create_done_null_result",{"action":"create","namespace":"qa","isolation_hint":"isolated","state":"done","result":None},TaskCreateRequest,"result"),
  ("archive_both_null",{"action":"archive","namespace":"qa","task_id":"t","note":None,"archive_note":None},TaskArchiveRequest,"archive_note"),
])
def test_public_manage_accepts_canonical_invalid_examples(key,payload,canonical,source_path):
    pub=next(t["inputSchema"] for t in CATALOGS["coordinator"] if t["name"]=="task_manage")
    assert not list(Draft202012Validator(pub).iter_errors(payload)),key
    assert TaskManageInput.model_validate(payload)
    with pytest.raises(ValidationError):
        canonical.model_validate({**payload,"code":None})

def test_valid_archive_note_fallback_and_optional_release_reason():
    from terminal_mcp.application.task_requests import TaskReleaseRequest
    data={"action":"archive","namespace":"qa","task_id":"t","note":"valid"}
    assert TaskArchiveRequest.model_validate({**data,"code":None})
    assert TaskReleaseRequest.model_validate({"action":"release","namespace":"qa","task_id":"t","code":None})



@pytest.mark.parametrize("item", json.loads((ROOT/"docs/compact-contracts/qa-bravo3-action-matrix.json").read_text())["rows"], ids=lambda x:x["action"])
def test_nine_action_forbidden_field_sets(item):
    from terminal_mcp.mcp.roles import _TASK_MANAGE_MODELS
    payload=item["forbidden_example"]
    model=_TASK_MANAGE_MODELS[item["action"]]
    pub=next(t["inputSchema"] for t in CATALOGS["coordinator"] if t["name"]=="task_manage")
    assert not list(Draft202012Validator(pub).iter_errors(payload))
    assert TaskManageInput.model_validate(payload)
    with pytest.raises(ValidationError) as exc:
        model.model_validate({**payload,"code":None})
    assert exc.value.errors(include_input=False)[0]["type"]=="extra_forbidden"

def test_claim_null_and_cross_action_extras():
    from terminal_mcp.application.task_requests import TaskClaimRequest,TaskReleaseRequest
    pub=next(t["inputSchema"] for t in CATALOGS["executor"] if t["name"]=="task_claim")
    cases=[
      (TaskClaimRequest,dict(action="claim",namespace="qa",task_id="t",claim_intent=None)),
      (TaskClaimRequest,dict(action="claim",namespace="qa",task_id="t",claim_intent="x",release_reason="other")),
      (TaskReleaseRequest,dict(action="release",namespace="qa",task_id="t",claim_intent="x")),
    ]
    for cls,payload in cases:
        assert not list(Draft202012Validator(pub).iter_errors(payload))
        with pytest.raises(ValidationError):
            cls.model_validate({**payload,"code":None})

def test_message_present_null_is_accepted_in_source():
    from terminal_mcp.mcp.access_contracts import MeshMessageInput
    for payload in [
        dict(action="ack",message_hash="h",text=None),
        dict(action="send",text="ok",message_hash=None),
        dict(action="read",text=None),
    ]:
        assert MeshMessageInput.model_validate(payload)

def test_update_null_omits_field_but_empty_list_preserved():
    from terminal_mcp.application.task_requests import TaskUpdateRequest,task_request_to_backend
    common=dict(action="update",namespace="qa",task_id="t",expected_revision=1,code=None)
    _,_,_,null_payload=task_request_to_backend(TaskUpdateRequest.model_validate({**common,"description":None}))
    _,_,_,empty_payload=task_request_to_backend(TaskUpdateRequest.model_validate({**common,"tags":[]}))
    assert "description" not in null_payload
    assert empty_payload["tags"] == []


def test_checkpoint_description_no_longer_advertises_expected_revision():
    item=next(t for t in CATALOGS["executor"] if t["name"]=="task_comment")
    assert "optional expected_revision" not in item["description"]
    assert "checkpoint does not accept expected_revision" in item["description"]
    payload={"action":"checkpoint","namespace":"qa","task_id":"t","checkpoint":"ok","expected_revision":1}
    assert not list(Draft202012Validator(item["inputSchema"]).iter_errors(payload))
    with pytest.raises(ValidationError) as exc:
        MeshTaskCommentInput.model_validate(payload)
    assert exc.value.errors(include_input=False)[0]["type"]=="extra_forbidden"


def test_relation_kind_open_ended_but_review_of_has_runtime_semantics():
    from terminal_mcp.application.task_requests import TaskRelateRequest
    item=next(t for t in CATALOGS["coordinator"] if t["name"]=="task_manage")
    payload={"action":"relate","namespace":"qa","task_id":"t",
             "relation_kind":"qa_arbitrary_rel","related_task_id":"u"}
    assert not list(Draft202012Validator(item["inputSchema"]).iter_errors(payload))
    assert TaskRelateRequest.model_validate({**payload,"code":None})
    assert "source lane must be review" in item["description"]
    assert "relation_kind is an open string" in item["description"]
