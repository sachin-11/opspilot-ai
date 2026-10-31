"""Client IP resolution behind trusted reverse proxies.

``X-Forwarded-For`` is attacker-controlled unless it was written by infrastructure we trust. The
header is therefore ignored unless the direct peer is inside ``TRUSTED_PROXY_CIDRS``; then the
chain is read right to left and the first address that is not a trusted proxy is the client, so
entries a caller prepends themselves can never win.
"""

import ipaddress
from collections.abc import Sequence

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Address = ipaddress.IPv4Address | ipaddress.IPv6Address

MAX_HEADER_CHARS = 2048
MAX_HOPS = 32


def parse_networks(values: Sequence[str]) -> list[Network]:
    networks: list[Network] = []
    for value in values:
        network = ipaddress.ip_network(value.strip(), strict=False)
        if network.prefixlen == 0:
            raise ValueError(f"trusted proxy network {value!r} would trust every client")
        networks.append(network)
    return networks


def _parse_address(raw: str) -> Address | None:
    text = raw.strip()
    if text.startswith("[") and "]" in text:  # [v6]:port
        text = text[1 : text.index("]")]
    elif text.count(":") == 1:  # v4:port
        text = text.split(":", 1)[0]
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _trusted(address: Address, networks: Sequence[Network]) -> bool:
    return any(address.version == network.version and address in network for network in networks)


def resolve_client_ip(
    peer: str | None, forwarded_for: str | None, trusted: Sequence[Network]
) -> str | None:
    """Return the best-known client address, never trusting an unvetted header."""
    if not peer:
        return None
    peer_address = _parse_address(peer)
    if peer_address is None or not trusted or not forwarded_for:
        return peer
    if not _trusted(peer_address, trusted):
        return peer
    # Keep the right-hand end: those entries were written by our own proxies. If the header had to
    # be cut, the first remaining entry may be partial, so it is dropped.
    cut = len(forwarded_for) > MAX_HEADER_CHARS
    parts = forwarded_for[-MAX_HEADER_CHARS:].split(",")
    hops = [hop for hop in (parts[1:] if cut else parts) if hop.strip()][-MAX_HOPS:]
    resolved: Address | None = None
    for raw in reversed(hops):
        address = _parse_address(raw)
        if address is None:
            return peer  # malformed chain: fall back to the nearest known address
        resolved = address
        if not _trusted(address, trusted):
            return str(address)
    # Every hop is a trusted proxy, so the leftmost one is the original (internal) client.
    return str(resolved) if resolved is not None else peer
