# Callback wire contract test

The test uses a controlled TLS listener bound only to IPv4 loopback on an
ephemeral port. It captures the plaintext HTTP/1.1 request after TLS decryption,
rather than replacing the sender with an in-process callback.

The unchanged production call chain is:

`post_signed` / `verify_callback` → production JSON serialization and signing
→ `_pinned_https_post` → `http.client.HTTPSConnection.request` → a real TLS
socket → the raw receiver.

## Independent oracle

The receiver uses the official Python Standard Webhooks reference implementation,
`standardwebhooks==1.1.0`, to verify captured request headers and body bytes.
The package is a test-only dependency. The known signing vector published in
upstream's `test_sign_function` also checks both the reference signer and the
production signer against a fixed expected signature.

- [Reference implementation](https://github.com/standard-webhooks/standard-webhooks/tree/main/libraries/python)
- [Published signing vector](https://github.com/standard-webhooks/standard-webhooks/blob/main/libraries/python/tests/test_webhooks.py)

There is no locally duplicated HMAC calculation in this test.

## Covered contract

- The complete HTTP/1.1 request is compared byte for byte, including the request
  line, all headers, separator and body
- POST method; default root path; escaped path and query; non-default port
- Host header: omitted default `:443` for both implicit and explicit URL forms,
  and preserved `:8443` for a non-default port
- Content-Type, webhook ID, timestamp, signature, subscription ID
- Compact JSON, literal Cyrillic/emoji UTF-8, and Content-Length in bytes
- Original callback hostname in TLS SNI
- Verification request payload and correct challenge response
- A 200 response with the wrong challenge is rejected by the production verifier
- Actual requests signed with stale/future timestamps or checked with the wrong
  receiver key are rejected by the reference receiver
- Modified captured body bytes and webhook ID are rejected by the independent
  verifier. This is a verifier-negative check, not simulated in-transit tampering
- Untrusted fixture CA and wrong TLS hostname fail before an HTTP request is sent

## Exact test boundary and limitations

The test replaces only the callback module's socket name/address selection with
a synthetic public DNS answer plus an asserted loopback-only address adapter.
It also replaces that module's SSL-context factory with a context that trusts
only the added ephemeral fixture CA (alongside normal platform roots).
Hostname checking and required certificate verification remain enabled.

These replacements are pytest-scoped module-local facades. They do not alter
global socket or SSL functions, production files, DNS, persistent trust stores,
or network settings. Fixture listeners are closed and threads joined in teardown.
Certificate material exists only in pytest's temporary directory.

This proves the observed production serialization, signing, HTTP send and
challenge-response contract under the stated fixture boundary. It does not
prove real public DNS behavior, real SSRF/address-selection behavior, the remote
platform's signing-key/subscription configuration, intermediary behavior, or
production platform acceptance. It is not an absolute guarantee.

## Run

Install the repository's test extra in an isolated development/test environment,
then run:

```sh
PYTHONPATH=src:. python -m pytest -q tests/test_mcp_callback_wire_contract.py
```

The test performs no public callback, requires no production secret, and deploys
nothing. The captured values are dummy fixture data.
