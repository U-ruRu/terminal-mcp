"""TMCP-01/RUN-02 independently verify contract immutability and nineteen tool records."""
import hashlib, json, pathlib, subprocess, sys
ROOT=pathlib.Path('/workspace/qa-tmcp01-run02-charlie3-20261010')
ART=ROOT/'docs/compact-contracts/generated'
OUT=pathlib.Path('/workspace/terminal-mcp-artifacts/charlie3-tmcp01-run02-20261010')
BASE='2649bc445252b8af9d34b6eee1dec2ce1ea6a601'
QA='36914f1fd19518ea0c40afb5eceadca0086230ca'
CAND='baaf7744a94ca126ba0b913b6829f98e2a33b693'
def data(ref,path):
    return subprocess.check_output(['git','show',f'{ref}:{path}'],cwd=ROOT)
def j(data):return json.loads(data)
def sha(data):return hashlib.sha256(data).hexdigest()
result={'candidate':CAND,'base':BASE,'checks':{},'tools':[]}
assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip()==CAND
changed=subprocess.check_output(['git','diff','--name-only',BASE,CAND],cwd=ROOT).decode().splitlines()
runtime_changes=[x for x in changed if x.startswith('src/') and x not in
    {'src/terminal_mcp/mcp/compact_annotations.json','src/terminal_mcp/mcp/compact_contracts.py'}]
assert not runtime_changes,runtime_changes
result['checks']['runtime_source_unmodified']=True
for basename in ('strict-contracts.json','before-access.json','before-executor.json','before-coordinator.json'):
    path=f'docs/compact-contracts/generated/{basename}'
    before=data(BASE,path);now=(ART/basename).read_bytes()
    assert before==now,(basename,sha(before),sha(now))
    result['checks'][f'{basename}_identical']=True
result['checks']['strict_sha256']=sha((ART/'strict-contracts.json').read_bytes())
assert result['checks']['strict_sha256']=='5bafb845096b00b43e2171024b467db8023808199ca5b27fa9f8642eabc7b694'
origpath='docs/compact-contracts/qa-tmcp01-bravo3-run01'
names=subprocess.check_output(['git','ls-tree','-r','--name-only',QA,origpath],cwd=ROOT).decode().splitlines()
assert len(names)==24,len(names)
for path in names:
    assert (ROOT/path).read_bytes()==data(QA,path),path
result['checks']['original_QA_24_files_preserved']=True
valid_names={'access':1,'executor':10,'coordinator':8}
sidecar=j((ART/'strict-contracts.json').read_bytes())
for role,expected_count in valid_names.items():
    path=f'docs/compact-contracts/generated/catalog-{role}.json'
    bef={t['name']:t for t in j(data(BASE,path))['tools']}
    aft={t['name']:t for t in j((ROOT/path).read_bytes())['tools']}
    assert len(aft)==expected_count==len(bef)
    for n,tool in aft.items():
        key=f'{role}.{n}'
        assert key in sidecar
        assert set(tool)==set(bef[n]),(key,'record keys drift')
        differences=[k for k in tool if tool[k]!=bef[n][k]]
        assert not set(differences)-{'description'},(key,differences)
        transport=tool['inputSchema']
        assert transport['type']=='object' and transport['additionalProperties'] is True
        assert all(v=={} for v in transport['properties'].values())
        assert set(transport['properties'])==set(sidecar[key]['inputSchema'].get('properties',{}))
        assert tool['outputSchema']==bef[n]['outputSchema']
        result['tools'].append({
          'name':key,'only_changed_field':differences,'before_description_chars':len(bef[n]['description']),
          'after_description_chars':len(tool['description']),'inputSchema_same':True,'outputSchema_same':True,
          'annotations_same':True,'public_transport_no_assertions':True,
        })
assert len(result['tools'])==19
result['checks']['all_19_non_description_fields_same']=True
result['checks']['changed_descriptions']=[x['name'] for x in result['tools'] if x['only_changed_field']]
result['checks']['all_19_tool_input_output_unchanged']=True
(OUT/'scope-parity-independent.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
print('PASS exact SHA, unchanged runtime source, strict sidecar, before-baselines, 24 prior QA files')
print('PASS all 19 tools, every public record field except descriptions identical')
print('DESCRIPTIONS CHANGED',result['checks']['changed_descriptions'])
print('STRICT SHA256',result['checks']['strict_sha256'])
