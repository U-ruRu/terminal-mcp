"""Independent remeasurement of committed offline catalogues, not builder output."""
from __future__ import annotations
import json
from pathlib import Path
import tiktoken

ROOT=Path(__file__).resolve().parents[1]
GEN=ROOT/"docs/compact-contracts/generated"
enc=tiktoken.get_encoding("cl100k_base")

def encode(obj):
    return json.dumps(obj,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")

def measure(obj):
    raw=encode(obj)
    return {"bytes":len(raw),"tokens":len(enc.encode(raw.decode(), disallowed_special=()))}

roles={}
total={key:0 for key in ("before_bytes","after_bytes","before_tokens","after_tokens")}
for role in ("access","executor","coordinator"):
    a=json.loads((GEN/f"before-{role}.json").read_text())
    b=json.loads((GEN/f"catalog-{role}.json").read_text())
    assert {t["name"] for t in a["tools"]}=={t["name"] for t in b["tools"]}
    x,y=measure(a),measure(b)
    entry={
        "count":len(b["tools"]),
        "before":x,"after":y,
        "diff_bytes":y["bytes"]-x["bytes"],"diff_tokens":y["tokens"]-x["tokens"],
        "parts":{"description":0,"inputSchema":0,"outputSchema":0,"annotations":0},
        "client_constraints":{"required":0,"extra_forbid":0,"conditional":0,
                              "enums":0,"lengths":0,"arrays":0,"numeric_bounds":0,
                              "types":0,"patterns":0,"unions":0},
    }
    for t in b["tools"]:
        for k in entry["parts"]:
            entry["parts"][k]+=measure(t.get(k,{}))["bytes"]
        def walk(v):
            if isinstance(v,dict):
                kw=v
                f=entry["client_constraints"]
                if kw.get("required"):f["required"]+=len(kw["required"])
                if kw.get("additionalProperties") is False:f["extra_forbid"]+=1
                if "if" in kw:f["conditional"]+=1
                if "enum" in kw or "const" in kw:f["enums"]+=1
                if "minLength" in kw or "maxLength" in kw:f["lengths"]+=1
                if "minItems" in kw or "maxItems" in kw or "uniqueItems" in kw:f["arrays"]+=1
                if "minimum" in kw or "maximum" in kw:f["numeric_bounds"]+=1
                if "type" in kw:f["types"]+=1
                if "pattern" in kw:f["patterns"]+=1
                if "oneOf" in kw or "anyOf" in kw:f["unions"]+=1
                for q in v.values():walk(q)
            elif isinstance(v,list):
                for q in v:walk(q)
        # Skip metadata action matrix because it isn't an input assertion.
        walk({k:v for k,v in t["inputSchema"].items() if k!="x-terminal-mcp-action-matrix"})
    roles[role]=entry
    for k in x:total[f"before_{k}"]+=x[k];total[f"after_{k}"]+=y[k]

report= {
    "candidate":"d1caea24e2d8c2dff650568fac616bcee0efb99b",
    "method":"Independent canonical Python JSON dumps UTF-8; tiktoken cl100k_base",
    "roles":roles,"sum":total,
    "delta":{"bytes":total["after_bytes"]-total["before_bytes"],
             "tokens":total["after_tokens"]-total["before_tokens"]},
}
assert total==dict(before_bytes=57221,after_bytes=54109,before_tokens=13763,after_tokens=12789)
path=ROOT/"docs/compact-contracts/qa-bravo3-independent-metrics.json"
path.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
for role,x in roles.items():
    print(role,x["count"],x["before"],x["after"],x["client_constraints"])
print("TOTAL",total,report["delta"])
