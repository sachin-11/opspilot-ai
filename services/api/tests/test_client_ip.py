from typing import Any

import pytest
from starlette.requests import Request

from opspilot_api.auth.clientip import parse_networks, resolve_client_ip
from opspilot_api.auth.dependencies import request_info

from .conftest import make_settings

PROXIES = parse_networks(["10.0.0.0/8", "fd00::/8"])


def test_header_is_ignored_when_no_proxy_is_trusted() -> None:
    assert resolve_client_ip("203.0.113.9", "198.51.100.1", []) == "203.0.113.9"


def test_header_is_ignored_when_the_peer_is_not_a_trusted_proxy() -> None:
    # A direct caller sets the header themselves; believing it would let them pick their own IP.
    assert resolve_client_ip("203.0.113.9", "198.51.100.1", PROXIES) == "203.0.113.9"


def test_client_is_the_first_untrusted_hop_from_the_right() -> None:
    assert resolve_client_ip("10.0.0.5", "198.51.100.1", PROXIES) == "198.51.100.1"
    assert resolve_client_ip("10.0.0.5", "198.51.100.1, 10.1.1.1", PROXIES) == "198.51.100.1"


def test_values_prepended_by_the_caller_cannot_win() -> None:
    # The edge appended the real client (198.51.100.1); the leading entry is attacker supplied.
    forged = "1.2.3.4, 198.51.100.1"
    assert resolve_client_ip("10.0.0.5", forged, PROXIES) == "198.51.100.1"


def test_all_hops_trusted_returns_the_original_internal_client() -> None:
    assert resolve_client_ip("10.0.0.5", "10.9.9.9, 10.1.1.1", PROXIES) == "10.9.9.9"


def test_malformed_or_missing_headers_fall_back_to_the_peer() -> None:
    assert resolve_client_ip("10.0.0.5", None, PROXIES) == "10.0.0.5"
    assert resolve_client_ip("10.0.0.5", "", PROXIES) == "10.0.0.5"
    assert resolve_client_ip("10.0.0.5", "not-an-ip", PROXIES) == "10.0.0.5"
    assert resolve_client_ip("10.0.0.5", "198.51.100.1, garbage", PROXIES) == "10.0.0.5"
    assert resolve_client_ip(None, "198.51.100.1", PROXIES) is None


def test_ports_ipv6_and_oversized_headers_are_handled() -> None:
    assert resolve_client_ip("10.0.0.5", "198.51.100.1:51234", PROXIES) == "198.51.100.1"
    assert resolve_client_ip("fd00::5", "2001:db8::7", PROXIES) == "2001:db8::7"
    assert resolve_client_ip("fd00::5", "[2001:db8::7]:443", PROXIES) == "2001:db8::7"
    huge = "198.51.100.1, " + ", ".join(["10.0.0.1"] * 10_000)
    assert resolve_client_ip("10.0.0.5", huge, PROXIES) == "10.0.0.1"  # bounded, no blow-up


def test_networks_must_be_valid_and_not_match_everything() -> None:
    for bad in ("0.0.0.0/0", "::/0"):
        with pytest.raises(ValueError, match="trust every client"):
            parse_networks([bad])
    with pytest.raises(ValueError):
        parse_networks(["not-a-cidr"])
    with pytest.raises(ValueError, match="trust every client"):
        make_settings(TRUSTED_PROXY_CIDRS="10.0.0.0/8, 0.0.0.0/0")


def test_settings_accept_a_comma_separated_list() -> None:
    settings = make_settings(TRUSTED_PROXY_CIDRS="10.0.0.0/8, 192.168.0.0/16")
    assert [str(network) for network in settings.trusted_proxy_networks] == [
        "10.0.0.0/8",
        "192.168.0.0/16",
    ]
    assert make_settings().trusted_proxy_networks == []


def _request(peer: str, forwarded: str | None, **settings: Any) -> Request:
    headers = [(b"user-agent", b"pytest")]
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))

    class App:
        class state:
            pass

    App.state.settings = make_settings(**settings)  # type: ignore[attr-defined]
    scope = {
        "type": "http",
        "headers": headers,
        "client": (peer, 1234),
        "app": App,
        "state": {"correlation_id": "req-1"},
    }
    return Request(scope)


def test_request_info_hashes_the_resolved_client_not_the_proxy() -> None:
    behind_proxy = request_info(
        _request("10.0.0.5", "198.51.100.1", TRUSTED_PROXY_CIDRS="10.0.0.0/8")
    )
    same_client_other_proxy = request_info(
        _request("10.0.0.6", "198.51.100.1", TRUSTED_PROXY_CIDRS="10.0.0.0/8")
    )
    other_client = request_info(
        _request("10.0.0.5", "198.51.100.2", TRUSTED_PROXY_CIDRS="10.0.0.0/8")
    )
    untrusted = request_info(_request("10.0.0.5", "198.51.100.1"))
    assert behind_proxy.ip_hash == same_client_other_proxy.ip_hash
    assert behind_proxy.ip_hash != other_client.ip_hash
    # Without trust configured, the header is ignored and every user behind the proxy looks alike.
    assert untrusted.ip_hash == request_info(_request("10.0.0.5", "198.51.100.2")).ip_hash
    assert untrusted.ip_hash != behind_proxy.ip_hash
