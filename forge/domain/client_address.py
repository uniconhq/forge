"""The address a request came from, as far as the proxy reports it. It is
displayed and never trusted, so an unparsable value becomes none.
"""

from ipaddress import IPv4Address, IPv6Address, ip_address


def client_address(value: str | None) -> IPv4Address | IPv6Address | None:
    if not value:
        return None
    try:
        return ip_address(value.strip())
    except ValueError:
        return None
