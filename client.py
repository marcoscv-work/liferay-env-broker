#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict

import requests

DEFAULT_BASE_URL = os.environ.get("LIFERAY_BROKER_URL", "")
DEFAULT_TOKEN = os.environ.get("LIFERAY_BROKER_TOKEN", "")
DEFAULT_USER = os.environ.get("LIFERAY_BROKER_USER", "")


def load_properties(path: str | None) -> str | None:
    if not path:
        return None
    return Path(path).read_text(encoding="utf-8")


def parse_env(pairs: list[str]) -> Dict[str, str]:
    env = {}
    for item in pairs:
        if "=" not in item:
            raise ValueError(f"Invalid variable format: {item}. Use KEY=VALUE")
        key, value = item.split("=", 1)
        env[key] = value
    return env


def request(method: str, path: str, token: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    headers["Content-Type"] = "application/json"
    url = f"{DEFAULT_BASE_URL.rstrip('/')}{path}"
    return requests.request(method, url, headers=headers, timeout=180, **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(description="Client for the local Liferay environment broker")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Create an environment")
    create.add_argument("--image", required=True)
    create.add_argument("--profile", default="standard")
    create.add_argument("--name")
    create.add_argument("--user", default=DEFAULT_USER, required=not bool(DEFAULT_USER))
    create.add_argument("--properties-file")
    create.add_argument("--port", type=int)
    create.add_argument("--ttl-hours", type=int)
    create.add_argument("--db-mode", choices=["none", "external"], default="none")
    create.add_argument("--env", action="append", default=[], help="KEY=VALUE")
    create.add_argument("--db-env", action="append", default=[], help="KEY=VALUE")

    sub.add_parser("list", help="List environments")
    status = sub.add_parser("status", help="Show one environment")
    status.add_argument("environment_id")
    delete = sub.add_parser("delete", help="Delete an environment")
    delete.add_argument("environment_id")
    touch = sub.add_parser("touch", help="Mark manual usage")
    touch.add_argument("environment_id")

    args = parser.parse_args()
    if not DEFAULT_BASE_URL:
        print("Set LIFERAY_BROKER_URL", file=sys.stderr)
        return 2
    token = DEFAULT_TOKEN
    if not token:
        print("Set LIFERAY_BROKER_TOKEN", file=sys.stderr)
        return 2

    try:
        if args.command == "create":
            payload = {
                "image": args.image,
                "profile": args.profile,
                "name": args.name,
                "user": args.user,
                "portal_properties": load_properties(args.properties_file),
                "host_port": args.port,
                "ttl_hours": args.ttl_hours,
                "db_mode": args.db_mode,
                "env": parse_env(args.env),
                "db_env": parse_env(args.db_env),
            }
            r = request("POST", "/v1/environments", token, data=json.dumps(payload))
        elif args.command == "list":
            r = request("GET", "/v1/environments", token)
        elif args.command == "status":
            r = request("GET", f"/v1/environments/{args.environment_id}", token)
        elif args.command == "touch":
            r = request("POST", f"/v1/environments/{args.environment_id}/touch", token)
        else:
            r = request("DELETE", f"/v1/environments/{args.environment_id}", token)
    except Exception as e:
        print(f"Connection error with broker: {e}", file=sys.stderr)
        return 1

    if r.status_code >= 400:
        print(f"Error {r.status_code}: {r.text}", file=sys.stderr)
        return 1

    print(json.dumps(r.json(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
