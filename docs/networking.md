# Network exposure

This page says what listens where, who can reach it, and what is deliberately closed. Nothing on the machine is reachable except what is listed under "Public".

## Who talks to whom

```text
                     internet
   Muse ──HTTPS──▶  [ ingress ]  ──HTTP, private network──▶  adapter  ──HTTPS──▶  MCP servers
                    caddy | tailscale funnel | cloudflare tunnel        (public internet only)

   you ──SSH (through IAP on Google Cloud)──▶ VM ──docker exec──▶ adapter CLI      (administration)
   you ──browser──▶ [ ingress ] ──▶ /connect, /oauth/callback, /c/<slug>/oauth/*    (sign-in pages)
```

## Public (reachable from the internet)

Only the ingress is public. It forwards to the adapter, which serves exactly these paths:

| Path | Who uses it | Authentication |
|---|---|---|
| `GET /health` | Monitors, the self-check | None; returns `{"status":"ok"}` |
| `GET /c/<slug>/openapi.json` | Muse, when adding the connector | None, but the address is unguessable (128-bit); contains no secrets |
| `POST /c/<slug>/operations/<id>` and `GET /c/<slug>/executions/…`, `/artifacts/…` | Muse | Bearer token for that connection |
| `/c/<slug>/oauth/*`, `/.well-known/…/c/<slug>` | Muse signing in | Connection code on the consent page; PKCE |
| `GET /connect/<nonce>`, `GET /oauth/callback` | You, signing in at an MCP provider | One-time nonce + browser-bound cookie + state |

Everything else returns 404. There are no administrative routes.

## Not public (and why)

| Component | Reachability |
|---|---|
| Adapter port 8080 | Not published to the host. Reachable only from the ingress container on the private compose network |
| Docker, host services | Not exposed; no other listening ports |
| SSH (port 22) | Google Cloud: only from Google's Identity-Aware Proxy range and only for authorized Google accounts. Existing machine: whatever you already allow |
| Database, secrets, backups | Files on the VM; no network interface |

## Choosing an ingress

| | Caddy | Tailscale Funnel | Cloudflare Tunnel |
|---|---|---|---|
| Public address | `https://<ip>.sslip.io` or your domain | `https://<host>.<tailnet>.ts.net` | Your hostname on Cloudflare |
| Inbound ports open | **80 and 443** | none | none |
| VM needs a public IP | Yes | No | No |
| TLS terminates at | The VM (Caddy) | The VM (Funnel passes encrypted traffic through Tailscale's relays) | **Cloudflare** — it can read traffic |
| Certificate | Automatic (Let's Encrypt) | Automatic | Cloudflare's |
| You need | Nothing extra | A Tailscale account | A Cloudflare account and a tunnel token |
| Best for | Simplest setup with a public VM | Home machine or no public IP, nothing to open | You already use Cloudflare |

Caddy sets HSTS, `nosniff`, frame denial, no referrer, and `no-store`, limits request bodies to 1 MiB, and writes no access log (so connector addresses are not recorded on disk).

## Outbound (what the adapter may reach)

| Rule | Detail |
|---|---|
| MCP servers | HTTPS only, to **public** addresses; DNS results are checked and pinned; redirects are not followed |
| Blocked at the network layer (VM) | Cloud metadata server (169.254.169.254), private ranges (10/8, 172.16/12, 192.168/16), carrier-grade NAT, link-local — by an egress guard that shares the adapter's network namespace |
| Exceptions | `--egress-allow CIDRS` at `up` time, for a server you run on a private network. Default: none |
| VM without public IP | Outbound goes through Cloud NAT; there is still no inbound path |

The guard is a second layer: the adapter refuses private destinations on its own, and the guard enforces the same at the packet level even if the adapter were compromised.

## Google Cloud network layout

- A dedicated VPC and subnet per deployment. The project's `default` network, with its permissive rules, is not used.
- Firewall: SSH from the IAP range only; 80/443 from anywhere only for Caddy. Nothing else is allowed in.
- Shielded VM, no service account (the VM has no cloud credentials to steal), OS Login on, project-wide SSH keys blocked, serial console off.
- Tunnel ingress: no external IP; Cloud NAT for outbound only.

## Known limitations

| Item | Detail |
|---|---|
| Caddy needs a public IP and open 80/443 | Choose a tunnel ingress to avoid it |
| `sslip.io` is a third-party DNS service | Used only to map an IP to a hostname for the certificate; use `--domain` to avoid it |
| Tunnel ingresses depend on their provider's availability | Caddy has no such dependency |
| Public paths can be probed | Each is protected as listed above; unknown paths return 404 and the sensitive ones need an unguessable address plus a token |
