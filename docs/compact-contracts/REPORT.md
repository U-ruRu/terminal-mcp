# Offline compact contracts — QA remediation report (Echo-3)

Parent: independently published Bravo-3 QA SHA 02b8f36a8855949bfb5f67ad568fdc0e40b1b0a9. Candidate branch: task/TMCP-RKB4-COMPACT-B1-B13-echo3-20261010.

**Scope:** B1–B13 in QA-BRAVO3-20261010.md, 19 tools, strict validation unchanged. Full source/cause/test matrix: **QA-REMEDIATION-ECHO3-20261010.md**.

New publication is **offline only**. It exposes a permissive MCP object transport schema with all real argument names while deriving complete business-rule text from strict models and verified annotations. The server remains the source of truth for allowed, mandatory, conditional, null and extra-field rules. The public flat output schema remains unchanged; the exact nested output/error contract is in generated/strict-contracts.json.

Original strict input/output sidecar SHA-256 (unchanged): 5bafb845096b00b43e2171024b467db8023808199ca5b27fa9f8642eabc7b694.

**Measured whole catalogues:** Access 1120 -> 2089 UTF-8 bytes; Executor 16009 -> 28334; Coordinator 40125 -> 28356. Total 57254 -> 58779 bytes (**+1525 / +2.66%**, no byte saving). Pinned tiktoken 0.12.0 cl100k_base total 13773 -> 13460 tokens (-313 / -2.27%). Per-tool and component shares in generated/metrics.json.

**Isolated real-MCP error samples:** 23 bad/stress calls (alongside 19 valid-boundary calls), with proposed tools/list installed **in memory only**. Complete HTTP JSON-RPC responses between 1016 and 2219 bytes; full result objects between 981 and 2183 bytes; nested error up to 659 bytes. No schema dump or long secret sentinel returned from server. Evidence qa-echo3-wire-metrics.json.

Existing independent Bravo-3 43 regression checks retained, B1–B13 dedicated checks added and additional role/schema tests rerun. Final exact counts and SHA are recorded in HANDOFF.md once all validations pass.

**Status: ready for independent OFFLINE re-review after final publish, NOT a live PASS.** Actual ChatGPT pre-tool rejection UI/schema dumps and real token accounting remain a separate A/B gate. The candidate was not deployed and canonical, OAuth, Events and other live tasks are unchanged. After later accepted message-receipt output changes, regeneration on that new integrated SHA is required.

