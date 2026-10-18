#!/usr/bin/env python3
"""Pair a Console device, read a snapshot, then exercise resumable WS transport."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import secrets
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect


def parse_pair_url(value: str) -> tuple[str, str, str]:
    """Decode only the current pairing contract; return server, Console origin, code."""
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.fragment:
        raise ValueError("pair URL must be HTTPS with an encoded fragment")
    if parsed.path != "/connect" or parsed.query or parsed.username or parsed.password:
        raise ValueError("invalid pairing URL path")
    if len(parsed.fragment) > 8192 or not re.fullmatch(r"[A-Za-z0-9_-]+", parsed.fragment):
        raise ValueError("invalid pairing fragment")
    try:
        padding = "=" * (-len(parsed.fragment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parsed.fragment + padding))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid encoded pairing fragment") from exc
    if not isinstance(payload, dict) or payload.get("v") != 2:
        raise ValueError("unsupported pairing version")
    server = payload.get("server")
    code = payload.get("code")
    if not isinstance(server, str) or not isinstance(code, str) or len(code) < 20:
        raise ValueError("invalid pairing data")
    server_url = urlsplit(server)
    if (
        server_url.scheme != "https"
        or not server_url.netloc
        or server_url.username
        or server_url.password
        or server_url.path not in ("", "/")
        or server_url.query
        or server_url.fragment
    ):
        raise ValueError("invalid paired server origin")
    console_origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return server_url.geturl().rstrip("/"), console_origin, code


def endpoint(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


def websocket_endpoint(base_url: str, ticket: str, since: int) -> str:
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = f"{parsed.path.rstrip('/')}/console/events"
    query = urlencode({"ticket": ticket, "since": since})
    return urlunsplit((scheme, parsed.netloc, path, query, ""))


async def checked_json(response: httpx.Response) -> dict:
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("expected a JSON object")
    return data


async def fetch_snapshot(client: httpx.AsyncClient, base_url: str, headers: dict[str, str]) -> dict:
    response = await client.get(endpoint(base_url, "/actions/console/snapshot"), headers=headers)
    return await checked_json(response)


async def issue_ticket(client: httpx.AsyncClient, base_url: str, headers: dict[str, str]) -> str:
    response = await client.post(endpoint(base_url, "/console/ws-ticket"), headers=headers)
    data = await checked_json(response)
    ticket = data.get("ticket")
    if not isinstance(ticket, str) or not ticket:
        raise RuntimeError("server returned no WebSocket ticket")
    return ticket


async def run(args: argparse.Namespace) -> int:
    base_url, origin, code = parse_pair_url(args.pair_url)
    async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout)) as client:
        pair_response = await client.post(
            endpoint(base_url, "/pairing/exchange"),
            headers={"Origin": origin},
            json={
                "code": code,
                "public_key": secrets.token_urlsafe(48),
                "device_label": args.device_label,
            },
        )
        pair = await checked_json(pair_response)
        access_token = pair.get("access_token")
        device_id = pair.get("device_id")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("pairing returned no access token")

        headers = {"Authorization": f"Bearer {access_token}", "Origin": origin}
        snapshot = await fetch_snapshot(client, base_url, headers)
        high_water = int(snapshot["high_water_seq"])
        cursor = high_water if args.since is None else args.since

        print(json.dumps({"stage": "paired", "device_id": device_id}, separators=(",", ":")))
        print(
            json.dumps(
                {"stage": "snapshot", "high_water_seq": high_water, "ws_since": cursor},
                separators=(",", ":"),
            )
        )

        for attempt in range(2):
            ticket = await issue_ticket(client, base_url, headers)
            uri = websocket_endpoint(base_url, ticket, cursor)
            async with connect(uri, origin=origin, open_timeout=args.timeout) as websocket:
                raw = await asyncio.wait_for(websocket.recv(), timeout=args.timeout)
                message = json.loads(raw)
                print(json.dumps({"stage": "ws", "message": message}, separators=(",", ":")))
                if message.get("type") != "resync_required":
                    return 0

            if attempt == 0:
                snapshot = await fetch_snapshot(client, base_url, headers)
                cursor = int(snapshot["high_water_seq"])
                print(
                    json.dumps(
                        {"stage": "resync", "high_water_seq": cursor},
                        separators=(",", ":"),
                    )
                )
                continue
            raise RuntimeError("server requested resync twice")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Terminal MCP Console smoke: pair -> snapshot -> WS replay/resync"
    )
    parser.add_argument("--pair-url", required=True)
    parser.add_argument("--device-label", default="console-transport-smoke")
    parser.add_argument(
        "--since",
        type=int,
        default=None,
        help="override snapshot cursor; 0 exercises replay/gap handling",
    )
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.since is not None and args.since < 0:
        parser.error("--since must be >= 0")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
