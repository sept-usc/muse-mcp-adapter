"""Set up a connector for the sample MCP server in one step.

    python examples/sample_mcp_server.py &                      # the MCP server
    python examples/prepare_sample_connector.py --public-url https://<your-tunnel-host>
    muse-mcp-adapter --database e2e/adapter.db --config e2e/serve.json serve

It registers the sample server, discovers it, publishes a policy that exposes everything (this is
a sample: review the draft policy before doing this with real data), links a connection, and
prints what to tell Muse. Run it again to republish after changing the sample server.
"""

import argparse
import contextlib
import io
import json
import sys
import time
from pathlib import Path

from muse_mcp_adapter.cli import main as cli


def _run(base: list[str], *arguments: str) -> object:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli([*base, *arguments])
    if code != 0:
        message = err.getvalue()
        if "already exists" in message:
            return None
        raise SystemExit(f"muse-mcp-adapter {' '.join(arguments[:1])} failed: {message.strip()}")
    return json.loads(out.getvalue()) if out.getvalue().strip() else None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--public-url", required=True, help="HTTPS address Muse can reach")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:9101/mcp")
    parser.add_argument("--workdir", default="e2e")
    parser.add_argument("--port", type=int, default=8080, help="local port for the adapter")
    args = parser.parse_args(argv)

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    config = workdir / "serve.json"
    config.write_text(
        json.dumps(
            {
                "publicUrl": args.public_url.rstrip("/"),
                "listen": {"host": "127.0.0.1", "port": args.port},
                # the sample MCP server is local and plain HTTP: development only
                "allowInsecureHttp": True,
                "allowPrivateNetworks": True,
            },
            indent=2,
        )
    )
    base = [
        "--database", str(workdir / "adapter.db"), "--config", str(config),
        "--allow-http", "--allow-private-network",
    ]  # fmt: skip

    _run(base, "server-register", "--server-id", "sample", "--display-name", "Sample issue tracker",
         "--endpoint", args.mcp_url, "--auth-profile", "none")  # fmt: skip
    _run(base, "discover-remote", "--server-id", "sample")
    draft = _run(base, "policy-template", "--server-id", "sample", "--expose-reads")
    assert isinstance(draft, list)
    for policy in draft:
        policy["allowed"] = True  # the sample is safe to expose in full
    policies = workdir / "policies.json"
    policies.write_text(json.dumps(draft, indent=2))

    version = f"1.{int(time.time())}"
    _run(base, "publish", "--server-id", "sample", "--catalog-version", version,
         "--policies", str(policies))  # fmt: skip
    _run(base, "activate", "--server-id", "sample", "--catalog-version", version)
    link = _run(base, "link", "--server-id", "sample", "--label", "Sample issue tracker",
                "--credential", "none")  # fmt: skip
    assert isinstance(link, dict)
    (workdir / "link.json").write_text(json.dumps(link, indent=2))

    print(f"Published catalog {version} with {len(draft)} operations.\n")
    print("1. Start the adapter:")
    print(f"   muse-mcp-adapter --database {workdir / 'adapter.db'} --config {config} serve\n")
    print("2. Expose it at the public URL you gave (for example with a tunnel to "
          f"http://127.0.0.1:{args.port}).\n")  # fmt: skip
    print("3. Tell Muse:\n")
    print(f"   {link['museInstructions']}\n")
    print(f"The connection code expires at {link['connectionCodeExpiresAt']}; "
          "issue a new one with `link-code`.")  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
