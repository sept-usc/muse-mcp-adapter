# Network exposure

This page says what listens where, who can reach it, and what is deliberately closed. Nothing on the machine is reachable from the internet except what is listed under "Public".

## Who talks to whom

```text
                     internet
   Muse ──HTTPS──▶  Tailscale Funnel  ──private network──▶  adapter  ──HTTPS──▶  MCP servers
                    (<name>.<tailnet>.ts.net)                                  (public internet only)

   you ──SSH (Google Cloud: through IAP)──▶ machine ──docker exec──▶ adapter CLI      (administration)
   you ──browser──▶ Tailscale Funnel ──▶ /connect, /oauth/callback, /c/<slug>/oauth/*  (sign-in pages)
```

The machine makes outbound connections to Tailscale, to Docker Hub and PyPI when it builds, and to the MCP servers. It accepts no inbound connection: Tailscale carries visitors in over the connection the machine opened.

## Public (reachable from the internet)

Only the Funnel address is public. It forwards to the adapter, which serves exactly these paths:

| Path | Who uses it | Authentication |
|---|---|---|
| `GET /health` | Monitors, the self-check | None; returns `{"status":"ok"}` |
| `GET /c/<slug>/openapi.json` | Muse, when adding the connector | None, but the address is unguessable (128-bit); it holds tool names and schemas, no secrets |
| `POST /c/<slug>/operations/<id>` and `GET /c/<slug>/executions/…`, `/artifacts/…` | Muse | Bearer token for that connection |
| `/c/<slug>/oauth/*`, `/.well-known/…/c/<slug>` | Muse signing in | Connection code on the consent page; PKCE |
| `GET /connect/<nonce>`, `GET /oauth/callback` | You, signing in at an MCP provider | One-time nonce, browser-bound cookie and state |

Everything else returns 404. There are no administrative routes, and every response carries `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and `Cache-Control: no-store`. Request bodies over 1 MiB are refused.

## Not public (and why)

| Component | Reachability |
|---|---|
| Adapter port 8080 | Not published to the host. Reachable only from the Tailscale container on the private compose network |
| Docker, host services | No other port is opened by the adapter |
| SSH (port 22) | Google Cloud: only from Google's Identity-Aware Proxy range, for authorized Google accounts. A machine you provide: whatever you already allow; the adapter needs no inbound port |
| Database, secrets, backups | Files on the machine; no network interface |

## What Tailscale can and cannot see

Funnel publishes a `*.ts.net` name through Tailscale's infrastructure. Per Tailscale's documentation, the HTTPS session ends on your machine, so Tailscale's relays carry encrypted traffic and the certificate is issued to your device. Tailscale does know that the device exists and which tailnet it belongs to. The machine joins your tailnet as one device, and under a default policy it can reach, and be reached by, your other tailnet devices. If you do not want that, restrict it in the Tailscale access controls (for example tag the device and allow only the Funnel attribute for the tag).

## Outbound: what the adapter may reach

| Rule | Detail |
|---|---|
| MCP servers | HTTPS only, to **public** addresses; DNS results are checked and pinned; redirects are not followed |
| Blocked at the network layer | Cloud metadata server (169.254.169.254), private ranges (10/8, 172.16/12, 192.168/16), carrier-grade NAT, link-local, enforced by the egress guard |
| Exceptions | `--egress-allow CIDRS` at `up` time, for a server you run on a private network. Default: none |
| Google Cloud VM | No public IP; outbound goes through Cloud NAT. There is no inbound path |

The guard is a second layer: the adapter refuses private destinations on its own, and the guard enforces the same at the packet level even if the adapter were compromised.

## Google Cloud network layout

- A dedicated VPC and subnet per deployment. The project's `default` network, with its permissive rules, is not used.
- One firewall rule: SSH from the IAP range only. No rule allows anything else in.
- Shielded VM, no service account (the VM has no cloud credentials to steal), OS Login on, project-wide SSH keys blocked, serial console off.
- No external IP; Cloud NAT for outbound only.

## Known limitations

| Item | Detail |
|---|---|
| Depends on Tailscale | The public address exists only while Tailscale's service is reachable and your tailnet allows Funnel |
| A new name takes minutes | Public DNS and the certificate for a first-time Funnel name can take up to ten minutes; `muse-vm up` waits |
| Funnel ports and bandwidth | Funnel serves port 443 and has bandwidth limits that are fine for an adapter and not for bulk transfer |
| Public paths can be probed | Each is protected as listed above; unknown paths return 404 and the sensitive ones need an unguessable address plus a token |
