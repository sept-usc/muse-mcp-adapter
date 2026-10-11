# Egress guard for the Muse MCP Adapter: installs nftables rules in the network namespace that the
# adapter joins. It needs NET_ADMIN; the adapter does not. The base image is pinned by digest.
FROM alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
RUN apk add --no-cache nftables
COPY guard.sh /usr/local/bin/mma-guard
RUN chmod 0755 /usr/local/bin/mma-guard
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD nft list table inet mma_guard >/dev/null 2>&1 && test -f /run/mma-guard.ready
ENTRYPOINT ["/usr/local/bin/mma-guard"]
