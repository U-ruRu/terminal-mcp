"""Explicit action-level differential review of nine coordinator mutations."""
from __future__ import annotations
import json
from pathlib import Path
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from terminal_mcp.mcp.roles import _TASK_MANAGE_MODELS
from terminal_mcp.mcp.role_contracts import TaskManageInput

ROOT=Path(__file__).resolve().parents[1]
catalog=json.loads((ROOT/"docs/compact-contracts/generated/catalog-coordinator.json").read_text())
pub=next(t["inputSchema"] for t in catalog["tools"] if t["name"]=="task_manage")
BASE={
    "create":dict(action="create",namespace="qa",isolation_hint="isolated"),
    "update":dict(action="update",namespace="qa",task_id="t",expected_revision=1),
    "checkpoint":dict(action="checkpoint",namespace="qa",task_id="t",checkpoint="progress"),
    "archive":dict(action="archive",namespace="qa",task_id="t",note="reason"),
    "review":dict(action="review",namespace="qa",task_id="t",dimensions=["A"],verdict="NON_BLOCKING"),
    "relate":dict(action="relate",namespace="qa",task_id="t",relation_kind="blocks",related_task_id="u"),
    "unrelate":dict(action="unrelate",namespace="qa",task_id="t",relation_kind="blocks",related_task_id="u"),
    "state":dict(action="state",namespace="qa",task_id="t",state="qa"),
    "comment":dict(action="comment",namespace="qa",task_id="t",comment_text="note"),
}
FORBIDDEN={
    "create":dict(archive_note="other"),
    "update":dict(checkpoint="out of place"),
    "checkpoint":dict(title="other"),
    "archive":dict(title="other"),
    "review":dict(title="other"),
    "relate":dict(title="other"),
    "unrelate":dict(title="other"),
    "state":dict(title="other"),
    "comment":dict(title="other"),
}
LOCATIONS={
    "create":"src/terminal_mcp/application/task_requests.py:118-178",
    "update":"src/terminal_mcp/application/task_requests.py:198-218",
    "checkpoint":"src/terminal_mcp/application/task_requests.py:220-223",
    "archive":"src/terminal_mcp/application/task_requests.py:265-291",
    "review":"src/terminal_mcp/application/task_requests.py:294-309",
    "relate":"src/terminal_mcp/application/task_requests.py:230-238",
    "unrelate":"src/terminal_mcp/application/task_requests.py:230-241",
    "state":"src/terminal_mcp/application/task_requests.py:244-246",
    "comment":"src/terminal_mcp/application/task_requests.py:225-228",
}
rows=[]
for action,base in BASE.items():
    cls=_TASK_MANAGE_MODELS[action]
    assert cls.model_validate({**base,"code":None})
    assert TaskManageInput.model_validate(base)
    assert not list(Draft202012Validator(pub).iter_errors(base))
    bad={**base,**FORBIDDEN[action]}
    assert TaskManageInput.model_validate(bad)
    assert not list(Draft202012Validator(pub).iter_errors(bad)),action
    try:cls.model_validate({**bad,"code":None})
    except ValidationError as e:
        err=e.errors(include_input=False,include_url=False)[0]
        assert err["type"]=="extra_forbidden",err
    else:raise AssertionError("canonical accepted forbidden action-field: "+action)
    required=[key for key,f in cls.model_fields.items() if f.is_required() if key!="code"]
    allowed=set(cls.model_fields)
    extras=sorted(set(TaskManageInput.model_fields)-allowed)
    rows.append(dict(action=action,model=cls.__name__,source=LOCATIONS[action],
                     required=required,canonical_allowed=sorted(allowed-{"code"}),
                     canonical_forbidden_public_fields=extras,forbidden_example=bad,
                     canonical_error={"path":str(err.get("loc")),"type":err["type"]},
                     public_schema_accepts=True,strict_top_level_accepts=True,canonical_accepts=False))
assert len(rows)==9
dest=ROOT/"docs/compact-contracts/qa-bravo3-action-matrix.json"
dest.write_text(json.dumps({"candidate":"d1caea24e2d8c2dff650568fac616bcee0efb99b",
                              "rows":rows},indent=2,ensure_ascii=False)+"\n")
for r in rows:
    print(r["action"],"required="+",".join(r["required"]),
          "forbidden_source_count="+str(len(r["canonical_forbidden_public_fields"])),
          "example="+str(r["forbidden_example"]))
