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
    assert MODULE.patch_caddyfile(
        original, site="example.test", upstream="127.0.0.1:8080"
    ) == original


def test_matcher_scoped_proxy_is_not_mistaken_for_catch_all():
    original = """example.test {
    @terminal path /actions/console/*
    handle @terminal {
        reverse_proxy 127.0.0.1:8080 {
            transport http {
                dial_timeout 5s
            }
        }
    }
    handle {
        respond "Not found" 404
    }
}
"""
    result = MODULE.patch_caddyfile(original, site="example.test", upstream="127.0.0.1:8080")

    assert result.count(MODULE.BEGIN) == 1
    assert "/actions/context" in result
    assert "/actions/persistent/*" in result
    assert "handle @terminal_mcp_console {" in result
    assert original.splitlines()[1] in result


def test_missing_site_appends_dedicated_catch_all_site():
    original = """other.test {
    respond "ok"
}
"""
    result = MODULE.patch_caddyfile(
        original, site="terminal.example.test", upstream="127.0.0.1:8080"
    )
    assert "terminal.example.test {" in result
    assert "reverse_proxy 127.0.0.1:8080" in result
    assert original in result


def test_public_site_requires_absolute_http_url():
    assert MODULE.public_site("https://example.test:8443/path") == "example.test:8443"
    try:
        MODULE.public_site("example.test")
    except ValueError as exc:
        assert "absolute http(s)" in str(exc)
    else:
        raise AssertionError("relative public URL must fail")
