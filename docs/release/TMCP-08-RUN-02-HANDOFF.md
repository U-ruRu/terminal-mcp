# TMCP-08 / RUN-02 — dot and next suite owner handoff

**Integrated candidate, NOT full release. Full-suite HOLD; canary NO-GO.**

1. Read TMCP-08-RUN-02-RELEASE-MANIFEST.md and external immutable SHA
   attestation in tmcp08-run02-foxtrot3-20261011/RELEASE-MANIFEST-IMMUTABLE.json.
   Revalidate remote SHA and clean checkout.
2. Obtain separate real NON_BLOCKING TMCP-11/RUN-01 verdict on snapshots.
   Foxtrot-3 did its own independent field-level schema QA, but this is
   not a substitute for separately assigned Golf-3 approval.
3. Dot explicitly allocates ONE new common pytest run to one owner on exact
   NEW SHA, checking process PID/argv and absence of old receipts or active
   pytest, with enough time to observe to genuine terminal exit. No test
   omission/xfail/deselection; RUNNING is never PASS.
4. Write a reviewed runner pinned to NEW SHA with corrected non-self-matching
   real process guard. The old prepared script pins 9c23bd6 and pgrep|grep
   self-matches; it cannot be run unchanged on the new candidate. Capture
   UTC, command, PYTHONPATH, PID, log, JUnit, exit, SHA and SHA256.
5. Compare any failure to the preserved prior 10 failures and baseline.
   Never mark PASS on exit 1. If clean full exit0, continue separate signed
   callback, A/B and FirstByte then BacLOUD operator-gated canaries with
   protected OAuth/key/anchor backup and rollback readiness.
6. Crash-atomicity of distinct SQLite files and real signed callback remain
   outside scope. No service/Canonical/OAuth storage/access changes were made.

Independent local verdicts on OAuth and additive schema: NON_BLOCKING.
Targeted former 10 = 10 PASS, affected six modules = 156 PASS, three-seed
generator --check PASS. Original failed full run remains immutable.
