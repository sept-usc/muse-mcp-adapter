# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately, not in a public issue or pull request.

Use GitHub's private vulnerability reporting: open the **Security** tab of this repository and choose **Report a vulnerability**. Include what you found, how to reproduce it, and the version or commit you tested.

You will get an acknowledgement, and we will tell you what we decide and when a fix is released. Please give us a reasonable chance to fix the issue before disclosing it publicly.

## Scope

In scope: the adapter, `muse-vm`, the container images and the deployment bundle in this repository, and the documentation where it could lead a user to an insecure setup.

Out of scope: vulnerabilities in third-party MCP servers, in Muse itself, or in services the adapter depends on (Tailscale, Docker, your cloud provider), which should be reported to their owners.

## How the adapter is meant to be protected

See [Security model](docs/security.md), [Identity and authentication](docs/authentication.md) and [Network exposure](docs/networking.md) for what is protected, how, and the known limitations.
