from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location("console_ingress", ROOT / "deploy" / "console_ingress.py")
assert SPEC and SPEC.loader
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_restrictive_site_gets_managed_console_routes_idempotently():
    original = """example.test {
    @legacy path /mcp /health/* /internal/fleet/*
    reverse_proxy @legacy 127.0.0.1:8080
}
"""
    once = MODULE.patch_caddyfile(original, site="example.test", upstream="127.0.0.1:8080")
    twice = MODULE.patch_caddyfile(once, site="example.test", upstream="127.0.0.1:8080")

    assert once == twice
    assert once.count(MODULE.BEGIN) == 1
    for path in MODULE.PATHS:
        assert path in once
    assert "/actions/context" in MODULE.PATHS
    assert "/actions/persistent/*" in MODULE.PATHS
    assert "@legacy path /mcp /health/* /internal/fleet/*" in once


def test_catch_all_proxy_is_already_console_ready():
    original = """example.test {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8080 {
        transport http {
            dial_timeout 5s
        }
    }
}
"""
    assert (
        MODULE.patch_caddyfile(original, site="example.test", upstream="127.0.0.1:8080") == original
    )
