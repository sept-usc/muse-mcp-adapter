# Day to day

| Task | Command |
|---|---|
| Health, containers, connected servers | `muse-vm status`, `muse-vm selfcheck`, `muse-vm servers` |
| Add another MCP server | `muse-vm add-server <name>` (see `muse-vm presets`); it gets its own connector and token |
| Change what Muse can use | `muse-vm expose SERVER --allow TOOL` / `--deny TOOL` |
| Logs | `muse-vm logs` (adapter), `muse-vm logs tailscale` |
| New version | `pipx upgrade muse-mcp-adapter` (or `git pull` in your checkout, and review what changed), then `muse-vm upgrade`: the address, connections and data stay |
| Backup and restore | `muse-vm backup --out FILE` (encrypted with a passphrase you choose), `muse-vm restore FILE` |
| A leaked token | `muse-vm rotate-token SERVER` |
| Pause to save money | `muse-vm stop`, `muse-vm start` (on Google Cloud this powers the VM off) |
| Remove everything | `muse-vm down` (signs the device out of Tailscale and deletes what was created) |

Reference for everything above: [deployment reference](deployment.md) (what is installed, every command), [running on a VM](operations.md), [identity and authentication](authentication.md) (every credential and how it is protected) and [network exposure](networking.md) (what is reachable from where).
