"""IP to country, offline and deliberately tiny.

A real deployment would plug in a GeoIP database or take the country from the edge (set `telemetry.ip_country`).
Here the documentation ranges stand in for countries so demos and simulators have something deterministic to use.
"""
import ipaddress

_TABLE = [
    (ipaddress.ip_network("198.51.100.0/24"), "US", "AS7922"),
    (ipaddress.ip_network("203.0.113.0/24"), "GB", "AS2856"),
    (ipaddress.ip_network("192.0.2.0/24"), "DE", "AS3320"),
    (ipaddress.ip_network("100.64.0.0/16"), "BR", "AS18881"),
    (ipaddress.ip_network("100.65.0.0/16"), "JP", "AS2516"),
    (ipaddress.ip_network("100.66.0.0/16"), "RU", "AS12389"),     # a "high risk" demo range
    (ipaddress.ip_network("100.67.0.0/16"), "NG", "AS37282"),     # a "high risk" demo range
    (ipaddress.ip_network("2001:db8::/32"), "US", "AS7922"),
]


def lookup(ip: str) -> tuple[str | None, str | None]:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None, None
    for net, country, asn in _TABLE:
        if addr in net:
            return country, asn
    return None, None
