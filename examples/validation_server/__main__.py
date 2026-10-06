"""Run Atlas:  python -m validation_server --auth none|bearer|oauth [--port 9101]."""

from __future__ import annotations

import argparse
from typing import cast

import uvicorn

from .app import AtlasOptions, build_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Atlas, an MCP server for validating the adapter")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9101)
    parser.add_argument("--auth", choices=("none", "bearer", "oauth"), default="none")
    parser.add_argument("--token", default="atlas-secret-token", help="token for --auth bearer")
    parser.add_argument(
        "--stateless",
        action="store_true",
        help="stateless HTTP (no sessions): sampling and elicitation cannot work",
    )
    parser.add_argument("--access-ttl", type=int, default=3600, help="OAuth access token lifetime")
    args = parser.parse_args()

    options = AtlasOptions(
        host=args.host,
        port=args.port,
        auth=cast("str", args.auth),  # type: ignore[arg-type]
        bearer_token=args.token,
        stateless=args.stateless,
        access_ttl_seconds=args.access_ttl,
    )
    atlas = build_app(options)
    base = f"http://{args.host}:{args.port}"
    print(f"Atlas MCP server ({args.auth} auth) at {base}/mcp ; control API at {base}/_control/")
    if args.auth == "bearer":
        print(f"Bearer token: {args.token}")
    uvicorn.run(atlas.asgi, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
