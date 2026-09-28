# Public repository privacy boundary

Terminal MCP is intended to be safe to publish without disclosing an operator's
infrastructure. Repository content and operational evidence therefore have
separate trust boundaries.

## Repository-safe data

Committed source, tests, fixtures, examples and product documentation use
environment-neutral values:

- server identities such as server-a, server-b and server-c;
- public examples under example.invalid, for example
  https://server-a.example.invalid;
- loopback addresses for local-only listeners;
- RFC documentation addresses only when an IP literal is actually required;
- generic product paths such as /opt/terminal-mcp, /etc/terminal-mcp,
  /var/lib/terminal-mcp and /var/cache/terminal-mcp;
- empty or explicit placeholder credentials.

Synthetic fixtures must be created for the test itself. Do not copy instance
names, agent IDs, credentials, addresses or logs from a running deployment into
a fixture.

## Operational-only data

Real hostnames, deployment labels, provider or account names, public and private
operator IP addresses, usernames, non-product filesystem locations, tokens,
keys and environment-specific identifiers stay outside Git. Supply them through
runtime configuration, secret files or non-repository operational evidence.

Do not add known real values to a repository deny-list. A deny-list would
publish the values it is meant to protect.

Local operational notes and evidence can use the ignored operator-evidence/,
runtime-evidence/ or .local-ops/ directories.

## Review and automated guardrail

Run:

    python3 scripts/check_repository_privacy.py

The default checker validates the structured public configuration examples,
synthetic fleet fixture and the current HEAD author/committer identity. Pytest
runs the same check. Commit identities must use an intentionally public
pseudonymous address under example.invalid or a GitHub noreply address. Never
allow Git to synthesize an identity from the machine hostname.

Unpublished task branches may retain older local-only ancestry until
integration. Final integration and release candidates must additionally run:

    python3 scripts/check_repository_privacy.py --reachable-history

That strict mode rejects unsafe metadata anywhere reachable from HEAD, so local
task history must be squashed or rewritten before the candidate is eligible to
push.

The automated check is deliberately based on positive repository-safe
conventions rather than a list of operator values. Human review remains
required for arbitrary prose and source changes: when a change mentions an
environment, verify that the value could have been invented without access to
a real deployment.
