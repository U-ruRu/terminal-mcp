# Terminal MCP prerelease versioning

The release version has three numeric components: `0.SCHEMA.CODE`.
The leading `0` means prerelease. Starting at `0.14.4`, the
**next public contract or database-schema change** gives `0.15.0`.
If the schemas stay the same but Python runtime code changes, the
next version is `0.14.5`. Documentation-only and identical rebuilds
keep their existing version.

The package version remains the numeric, PEP 440-compatible value.
An optional seven-character Git SHA follows the visible release ID
after a hyphen, e.g. `0.15.0-4ee3f15`. The current build is available
at `/health/live` as `version` and `release`, and in
`/opt/terminal-mcp/current/RELEASE_META.json`.

## How builds are classified

`deploy/install.sh update` derives the version automatically from the
last installed release and a **disposable copy** of the candidate source.
It compares the published Access Mesh and role MCP schema manifests and
the runtime/authentication SQLite schema revision numbers. A changed
schema increments the second component and resets the last component.
Other changes to Python package source files increment the third
component. Build-time stamping never edits the canonical Git worktree,
and the prior installed release remains untouched.

Keep the generated MCP baseline manifests current with the runtime:
schema-contract tests must pass before release. Every SQLite schema
migration must update its explicit schema revision. After a QA test
deployment, the installer uses the most recent confirmed canonical
release as the version baseline rather than incrementing from an
unreleased QA build.

For builds from a source tree with `.git`, the SHA is detected.
For Git archives, name the extracted directory after its Git SHA or
supply `TERMINAL_MCP_SOURCE_SHA=<commit>`. The latter is preferred in
automation. An unknown commit identity leaves off the suffix rather
than inventing a Git SHA.

To label staging distinctly from a final promotion, set
`TERMINAL_MCP_DEPLOY_CHANNEL=qa` or `=canonical`. QA updates inherit
the QA channel from the previous release. Promoting an approved
candidate to canonical requires setting `canonical` explicitly if
the currently active build is QA.

Manual check:

```bash
python3 scripts/release_version.py --source . --source-repo .
```

Build source stamping is reserved for the installer or an isolated
non-Git source copy using `--stamp --output <path>`.
