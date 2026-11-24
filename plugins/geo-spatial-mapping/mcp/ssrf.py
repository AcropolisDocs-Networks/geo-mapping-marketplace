"""SSRF guard for the bundled OGC MCP server (spec 8.2, 14).

A host is allowed only when every address it resolves to is a public unicast address. Loopback, private,
link-local, multicast, reserved, unspecified and cloud-metadata addresses are always refused. The check is
repeated for every redirect hop by the caller. The server is read-only and holds no credentials.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

METADATA = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")}


class Refused(Exception):
    """The target is not allowed. The message names the reason, never an internal address."""


def _bad(ip: ipaddress._BaseAddress) -> bool:
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or ip in METADATA or getattr(ip, "is_site_local", False))


def public_addresses(url: str, resolver=socket.getaddrinfo) -> list[str]:
    """Validate a URL and return the public addresses it resolves to. Raises Refused. https and http only, no credentials in the URL."""
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise Refused("only http and https URLs are allowed")
    if parts.username or parts.password:
        raise Refused("credentials in a URL are not allowed")
    host = parts.hostname
    if not host:
        raise Refused("URL has no host")
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = resolver(host, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except OSError:
            raise Refused("host did not resolve")
        addrs = [ipaddress.ip_address(i[4][0]) for i in infos]
    if not addrs:
        raise Refused("host did not resolve")
    if any(_bad(a) for a in addrs):
        raise Refused("target is not a public address (private, loopback, link-local and metadata ranges are refused)")
    return [str(a) for a in addrs]


def check_url(url: str, resolver=socket.getaddrinfo) -> str:
    """Return the validated URL or raise Refused."""
    public_addresses(url, resolver)
    return url
