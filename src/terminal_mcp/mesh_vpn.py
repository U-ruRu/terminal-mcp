"""Operator-controlled WireGuard Mesh bootstrap and Linux transport lifecycle.

A node is trusted only after verification of its signed offer against the
existing Fleet identity, or explicit operator pinning on first enrollment.
Private WireGuard keys are generated and stay on their issuer host.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from terminal_mcp.fleet.config import _instance_id, _key_bytes, build_fleet_config

BACKENDS = ("auto", "kernel", "userspace")
DEFAULT_STATE_DIR = "/etc/terminal-mcp/mesh-vpn"
DEFAULT_OVERRIDES = "/etc/terminal-mcp/fleet-peer-transports.json"
_IFACE = re.compile(r"^[A-Za-z0-9_-]{1,15}$")


class VPNError(ValueError):
    pass


def _run(*argv: str, input_data: bytes | None = None) -> bytes:
    try:
        return subprocess.run(
            argv, input=input_data, capture_output=True, check=True, timeout=12
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as exc:
        raise VPNError(f"WireGuard operation failed: {argv[0]}") from exc


def _atomic_json(path: Path, value: object, *, exclusive: bool = False) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        return
    temp = path.with_name(path.name + ".tmp-" + os.urandom(6).hex())
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VPNError(f"invalid or missing file: {path}") from exc
    if not isinstance(data, dict):
        raise VPNError("configuration must be a JSON object")
    return data


def _address(value: str) -> str:
    try:
        ip = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise VPNError("WireGuard overlay must be a private IPv4 address") from exc
    if not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        raise VPNError("WireGuard overlay must be a private IPv4 address")
    return str(ip)


def _endpoint(value: str) -> str:
    """Static public IPv4 endpoint avoids DNS rebinding in bootstrap manifests."""
    host, sep, port = value.rpartition(":")
    if not sep:
        raise VPNError("endpoint must be an IPv4:port")
    try:
        ip = ipaddress.IPv4Address(host)
        number = int(port)
    except ValueError as exc:
        raise VPNError("endpoint must be an IPv4:port") from exc
    if ip.is_unspecified or ip.is_multicast or not 1 <= number <= 65535:
        raise VPNError("invalid WireGuard endpoint")
    return f"{ip}:{number}"


def _key(value: str, length: int = 32) -> bytes:
    try:
        data = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise VPNError("invalid base64 key") from exc
    if len(data) != length:
        raise VPNError("invalid key length")
    return data


def fingerprint(public_key: str) -> str:
    return hashlib.sha256(_key(public_key)).hexdigest()


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _settings():
    # The same trusted installed Fleet identity used by the application.
    from terminal_mcp.__main__ import _local_settings

    return _local_settings()


def init(
    state_dir: Path,
    *,
    instance_id: str,
    backend: str,
    interface: str,
    overlay_ip: str,
    endpoint: str,
    listen_port: int,
    proxy_port: int,
) -> dict:
    _instance_id(instance_id, "instance_id")
    if backend not in BACKENDS or not _IFACE.fullmatch(interface):
        raise VPNError("invalid backend or interface")
    if not 1 <= listen_port <= 65535 or not 1024 <= proxy_port <= 65535:
        raise VPNError("invalid port")
    overlay_ip = _address(overlay_ip)
    endpoint = _endpoint(endpoint)
    private_path = state_dir / "private.key"
    if (state_dir / "local.json").exists() or private_path.exists():
        raise VPNError("already initialized; use existing local WireGuard identity")
    state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(state_dir, 0o700)
    private = _run("wg", "genkey") + b"\n"
    public = _run("wg", "pubkey", input_data=private).decode()
    _key(public)
    fd = os.open(private_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(private)
    settings = {
        "version": 1,
        "instance_id": instance_id,
        "backend": backend,
        "interface": interface,
        "overlay_ip": overlay_ip,
        "endpoint": endpoint,
        "listen_port": listen_port,
        "proxy_port": proxy_port,
        "wireguard_public_key": public,
    }
    _atomic_json(state_dir / "local.json", settings, exclusive=True)
    return settings


def create_offer(local: dict, signing_private_key: str, *, now: int | None = None) -> dict:
    now = int(time.time() if now is None else now)
    signer = Ed25519PrivateKey.from_private_bytes(
        _key_bytes(signing_private_key, "fleet_signing_private_key")
    )
    pub = signer.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    body = {
        "version": 1,
        "instance_id": local["instance_id"],
        "wireguard_public_key": local["wireguard_public_key"],
        "fleet_signing_public_key": base64.b64encode(pub).decode(),
        "overlay_ip": local["overlay_ip"],
        "endpoint": local["endpoint"],
        "proxy_port": local["proxy_port"],
        "issued_at": now,
        "expires_at": now + 600,
        "nonce": os.urandom(12).hex(),
    }
    return {
        "payload": body,
        "signature": base64.b64encode(signer.sign(_canonical(body))).decode(),
    }


def verify_offer(
    offer: dict,
    *,
    expected_instance_id: str,
    pinned_key: str | None = None,
    approved_fingerprint: str | None = None,
    now: int | None = None,
) -> dict:
    now = int(time.time() if now is None else now)
    if set(offer) != {"payload", "signature"} or not isinstance(offer["payload"], dict):
        raise VPNError("invalid enrollment offer")
    body = offer["payload"]
    required = {
        "version",
        "instance_id",
        "wireguard_public_key",
        "fleet_signing_public_key",
        "overlay_ip",
        "endpoint",
        "proxy_port",
        "issued_at",
        "expires_at",
        "nonce",
    }
    if set(body) != required or body["version"] != 1:
        raise VPNError("invalid enrollment schema")
    if body["instance_id"] != expected_instance_id:
        raise VPNError("enrollment node identity mismatch")
    _instance_id(expected_instance_id, "peer instance_id")
    _key(body["wireguard_public_key"])
    _address(body["overlay_ip"])
    _endpoint(body["endpoint"])
    if type(body["proxy_port"]) is not int or not 1024 <= body["proxy_port"] <= 65535:
        raise VPNError("invalid proxy port")
    if not isinstance(body["nonce"], str) or not re.fullmatch(r"[0-9a-f]{24}", body["nonce"]):
        raise VPNError("invalid enrollment nonce")
    if (
        type(body["issued_at"]) is not int
        or type(body["expires_at"]) is not int
        or body["issued_at"] > now + 60
        or body["expires_at"] < now
        or body["expires_at"] - body["issued_at"] > 600
    ):
        raise VPNError("expired or invalid enrollment offer")
    try:
        signed_pub = _key(body["fleet_signing_public_key"])
        if pinned_key:
            if signed_pub != _key_bytes(pinned_key, "pinned Fleet signing key"):
                raise VPNError("Fleet signing identity mismatch")
        elif (
            not approved_fingerprint
            or fingerprint(body["fleet_signing_public_key"]) != approved_fingerprint
        ):
            raise VPNError("new Fleet identity requires independently confirmed SHA256 fingerprint")
        Ed25519PublicKey.from_public_bytes(signed_pub).verify(
            _key(offer["signature"], 64), _canonical(body)
        )
    except VPNError:
        raise
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise VPNError("invalid signed enrollment offer") from exc
    return body


def join(
    state_dir: Path,
    offer: dict,
    *,
    peer_id: str,
    approved_fingerprint: str | None,
    pinned_key: str | None,
    now: int | None = None,
) -> dict:
    local = _load(state_dir / "local.json")
    if peer_id == local["instance_id"]:
        raise VPNError("self enrollment is not permitted")
    body = verify_offer(
        offer,
        expected_instance_id=peer_id,
        pinned_key=pinned_key,
        approved_fingerprint=approved_fingerprint,
        now=now,
    )
    if body["overlay_ip"] == local["overlay_ip"]:
        raise VPNError("overlay address collision")
    if body["endpoint"] == local["endpoint"]:
        raise VPNError("underlay endpoint collision")
    path = state_dir / "peers" / f"{peer_id}.json"
    if path.exists():
        old = _load(path)
        if old != body:
            # The replayable timestamp/nonce may change without key rotation.
            fixed = (
                "wireguard_public_key",
                "fleet_signing_public_key",
                "overlay_ip",
                "endpoint",
                "proxy_port",
            )
            if any(old.get(field) != body[field] for field in fixed):
                raise VPNError("peer key or route changed; rotate with operator approval")
        else:
            return body
    _atomic_json(path, body)
    return body


def _peers(state_dir: Path) -> list[dict]:
    return [_load(f) for f in sorted((state_dir / "peers").glob("*.json"))]


def _backend(interface: str, preference: str) -> str:
    if preference in {"kernel", "auto"}:
        completed = subprocess.run(
            ["ip", "link", "add", "dev", interface, "type", "wireguard"],
            capture_output=True,
            check=False,
            timeout=12,
        )
        if completed.returncode == 0:
            return "kernel"
        if preference == "kernel":
            raise VPNError("kernel WireGuard requested, but interface creation failed")
    if preference not in {"userspace", "auto"}:
        raise VPNError("invalid backend")
    if shutil.which("wireguard-go") is None:
        raise VPNError("userspace WireGuard requires installed wireguard-go")
    _run("wireguard-go", interface)
    return "userspace"


def up(state_dir: Path, *, backend: str | None = None) -> str:
    local = _load(state_dir / "local.json")
    mode = backend or local["backend"]
    if mode not in BACKENDS:
        raise VPNError("invalid backend")
    peers = _peers(state_dir)
    if not peers:
        raise VPNError("at least one enrolled peer is required")
    interface = local["interface"]
    if not _IFACE.fullmatch(interface):
        raise VPNError("invalid interface")
    existing = subprocess.run(["ip", "link", "show", "dev", interface], capture_output=True)
    if existing.returncode == 0:
        raise VPNError("interface already exists; use status or down first")
    selected = _backend(interface, mode)
    try:
        _run(
            "wg",
            "set",
            interface,
            "private-key",
            str(state_dir / "private.key"),
            "listen-port",
            str(local["listen_port"]),
        )
        for peer in peers:
            _run(
                "wg",
                "set",
                interface,
                "peer",
                peer["wireguard_public_key"],
                "allowed-ips",
                f"{_address(peer['overlay_ip'])}/32",
                "endpoint",
                _endpoint(peer["endpoint"]),
                "persistent-keepalive",
                "25",
            )
        _run("ip", "address", "add", f"{_address(local['overlay_ip'])}/32", "dev", interface)
        _run("ip", "link", "set", interface, "up")
        for peer in peers:
            # Never create a default route or change existing host Internet routing.
            _run("ip", "route", "add", f"{peer['overlay_ip']}/32", "dev", interface)
    except Exception:
        subprocess.run(["ip", "link", "del", "dev", interface], capture_output=True)
        raise
    return selected


def down(state_dir: Path) -> None:
    local = _load(state_dir / "local.json")
    interface = local["interface"]
    # A same-named unrelated WireGuard interface must never be removed.
    result = subprocess.run(["wg", "show", interface, "public-key"], capture_output=True, text=True)
    if result.returncode == 0:
        if result.stdout.strip() != local["wireguard_public_key"]:
            raise VPNError("refusing to remove an interface with a different WireGuard identity")
        _run("ip", "link", "del", "dev", interface)


def status(state_dir: Path) -> dict:
    local = _load(state_dir / "local.json")
    interface = local["interface"]
    result = subprocess.run(
        ["wg", "show", interface, "latest-handshakes"], capture_output=True, text=True
    )
    return {
        "instance_id": local["instance_id"],
        "interface": interface,
        "configured_backend": local["backend"],
        "running": result.returncode == 0,
        "peer_count": len(_peers(state_dir)),
        "handshakes": {
            fingerprint(peer["wireguard_public_key"])[:12]: next(
                (
                    int(line.split()[1])
                    for line in result.stdout.splitlines()
                    if line.split()[0] == peer["wireguard_public_key"]
                ),
                0,
            )
            for peer in _peers(state_dir)
        }
        if result.returncode == 0
        else {},
    }


def switch(
    state_dir: Path,
    path: Path,
    *,
    peer_id: str,
    mode: str,
    probe: bool = True,
    now: int | None = None,
) -> dict:
    local = _load(state_dir / "local.json")
    if mode not in {"https", "wireguard"}:
        raise VPNError("mode must be https or wireguard")
    if mode == "wireguard":
        peer = next((p for p in _peers(state_dir) if p["instance_id"] == peer_id), None)
        if peer is None:
            raise VPNError("peer must be enrolled before WireGuard switch")
        if probe:
            observed = status(state_dir)
            target = fingerprint(peer["wireguard_public_key"])[:12]
            handshake = observed["handshakes"].get(target, 0)
            if not observed["running"] or not (
                handshake and 0 <= int(time.time() if now is None else now) - handshake <= 180
            ):
                raise VPNError("WireGuard handshake not fresh; refusing transport switch")
            response = httpx.get(
                f"http://{peer['overlay_ip']}:{peer['proxy_port']}/health/live", timeout=3
            )
            if response.status_code != 200:
                raise VPNError("WireGuard application health probe failed")
        override = {
            "mode": "wireguard",
            "origin": f"http://{peer['overlay_ip']}:{peer['proxy_port']}",
            "interface": local["interface"],
        }
    result = _load(path) if path.exists() else {}
    if mode == "https":
        result.pop(peer_id, None)
    else:
        result[peer_id] = override
    _atomic_json(path, result)
    return {"peer": peer_id, "transport": mode, "restart_required": True}


async def proxy_server(*, overlay_ip: str, port: int, upstream_port: int) -> None:
    """Bind the internal HTTP service only on the private tunnel address."""
    semaphore = asyncio.Semaphore(64)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        async with semaphore:
            try:
                other_reader, other_writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", upstream_port), 5
                )

                async def pipe(src, dst):
                    try:
                        while data := await asyncio.wait_for(src.read(65536), 120):
                            dst.write(data)
                            await dst.drain()
                    except (TimeoutError, ConnectionError, OSError):
                        pass
                    finally:
                        dst.close()

                await asyncio.gather(pipe(reader, other_writer), pipe(other_reader, writer))
            except (OSError, TimeoutError):
                pass
            finally:
                writer.close()

    server = await asyncio.start_server(handle, overlay_ip, port)
    async with server:
        await server.serve_forever()


def set_backend(state_dir: Path, preference: str) -> dict:
    if preference not in BACKENDS:
        raise VPNError("invalid backend")
    data = _load(state_dir / "local.json")
    result = subprocess.run(["wg", "show", data["interface"], "public-key"], capture_output=True)
    if result.returncode == 0:
        raise VPNError("stop the WireGuard interface before changing its backend")
    data["backend"] = preference
    _atomic_json(state_dir / "local.json", data)
    return {"backend": preference, "restart_required": True}


def install_units(
    state_dir: Path, unit_dir: Path, executable: str = "/opt/terminal-mcp/current/bin/terminal-mcp"
) -> list[str]:
    """Install opt-in service units; no systemd activation or production mutation."""
    for value in (str(state_dir), str(unit_dir), executable):
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", value) or not value.startswith("/"):
            raise VPNError("service paths must be absolute, without special characters")
    if not Path(executable).is_absolute():
        raise VPNError("terminal-mcp executable must be an absolute path")
    local = _load(state_dir / "local.json")
    _address(local["overlay_ip"])
    interface_unit = f"""[Unit]
Description=Terminal MCP WireGuard Mesh tunnel
Wants=network-online.target
After=network-online.target
Before=terminal-mcp.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={executable} mesh-vpn up --state-dir {state_dir}
ExecStop={executable} mesh-vpn down --state-dir {state_dir}
TimeoutStartSec=30
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""
    proxy_unit = f"""[Unit]
Description=Terminal MCP private WireGuard ingress
Requires=terminal-mcp-mesh-vpn.service
After=terminal-mcp-mesh-vpn.service

[Service]
Type=simple
ExecStart={executable} mesh-vpn serve --state-dir {state_dir}
Restart=on-failure
RestartSec=2
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
CapabilityBoundingSet=
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

[Install]
WantedBy=multi-user.target
"""
    files = {
        "terminal-mcp-mesh-vpn.service": interface_unit,
        "terminal-mcp-mesh-vpn-proxy.service": proxy_unit,
    }
    for name, content in files.items():
        path = unit_dir / name
        if path.exists() and path.read_text() != content:
            raise VPNError(f"unit exists with different content: {name}")
    for name, content in files.items():
        path = unit_dir / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(content)
    return list(files)


def cli(args: argparse.Namespace) -> dict | None:
    directory = Path(args.state_dir)
    if args.action == "init":
        settings = _settings()
        return init(
            directory,
            instance_id=settings.fleet_instance_id,
            backend=args.backend,
            interface=args.interface,
            overlay_ip=args.overlay_ip,
            endpoint=args.endpoint,
            listen_port=args.listen_port,
            proxy_port=args.proxy_port,
        )
    if args.action == "backend":
        return set_backend(directory, args.mode)
    if args.action == "install-service":
        units = install_units(directory, Path(args.unit_dir), args.executable)
        if args.enable:
            _run("systemctl", "daemon-reload")
            _run("systemctl", "enable", "--now", *units)
        return {"units": units, "enabled": bool(args.enable)}
    if args.action == "offer":
        settings = _settings()
        return create_offer(_load(directory / "local.json"), settings.fleet_signing_private_key)
    if args.action == "join":
        settings = _settings()
        config = build_fleet_config(settings)
        peer = config.peers_by_id.get(args.peer) if config else None
        return join(
            directory,
            _load(Path(args.manifest)),
            peer_id=args.peer,
            pinned_key=peer.public_key if peer else None,
            approved_fingerprint=args.approve_fingerprint,
        )
    if args.action == "up":
        return {"backend": up(directory, backend=args.backend)}
    if args.action == "down":
        down(directory)
        return {"stopped": True}
    if args.action == "status":
        return status(directory)
    if args.action == "switch":
        config = build_fleet_config(_settings())
        if not config or args.peer not in config.peers_by_id:
            raise VPNError("switch requires an already trusted Fleet peer")
        return switch(directory, Path(args.transports_path), peer_id=args.peer, mode=args.mode)
    if args.action == "serve":
        local = _load(directory / "local.json")
        asyncio.run(
            proxy_server(
                overlay_ip=local["overlay_ip"],
                port=local["proxy_port"],
                upstream_port=args.upstream_port,
            )
        )
        return None
    raise VPNError("unknown mesh-vpn operation")


def add_cli(parser: argparse.ArgumentParser) -> None:
    root = parser.add_subparsers(dest="action", required=True)
    for action in (
        "init",
        "offer",
        "join",
        "up",
        "down",
        "status",
        "switch",
        "serve",
        "backend",
        "install-service",
    ):
        sub = root.add_parser(action)
        sub.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
        if action == "init":
            sub.add_argument("--backend", choices=BACKENDS, default="auto")
            sub.add_argument("--interface", default="tmcpwg")
            sub.add_argument("--overlay-ip", required=True)
            sub.add_argument("--endpoint", required=True)
            sub.add_argument("--listen-port", type=int, default=53148)
            sub.add_argument("--proxy-port", type=int, default=18080)
        if action == "backend":
            sub.add_argument("--mode", choices=BACKENDS, required=True)
        if action == "install-service":
            sub.add_argument("--unit-dir", default="/etc/systemd/system")
            sub.add_argument("--executable", default="/opt/terminal-mcp/current/bin/terminal-mcp")
            sub.add_argument("--enable", action="store_true")
        if action == "join":
            sub.add_argument("--manifest", required=True)
            sub.add_argument("--peer", required=True)
            sub.add_argument("--approve-fingerprint", default=None)
        if action == "up":
            sub.add_argument("--backend", choices=BACKENDS, default=None)
        if action == "switch":
            sub.add_argument("--peer", required=True)
            sub.add_argument("--mode", choices=("https", "wireguard"), required=True)
            sub.add_argument("--transports-path", default=DEFAULT_OVERRIDES)
        if action == "serve":
            sub.add_argument("--upstream-port", type=int, default=8080)
