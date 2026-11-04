"""PAYBROKER_DEV_WEBHOOK_HOSTS: exact host names a local stack's webhook receivers live at, honoured only with PAYBROKER_DEV_HTTP, never for
link-local, never as a wildcard. Everything else keeps the SSRF guard of PAYBROKER_ALLOW_PRIVATE_URLS."""
import logging

import pytest

from paybroker import netguard
from tests.conftest import needs_db

PRIVATE = [(2, 1, 6, "", ("172.18.0.5", 8104))]
LINK_LOCAL = [(2, 1, 6, "", ("169.254.169.254", 80))]
PUBLIC = [(2, 1, 6, "", ("93.184.216.34", 443))]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in ("PAYBROKER_DEV_HTTP", "PAYBROKER_DEV_WEBHOOK_HOSTS", "PAYBROKER_ALLOW_PRIVATE_URLS"):
        monkeypatch.delenv(v, raising=False)


def resolving(monkeypatch, infos):
    monkeypatch.setattr("paybroker.netguard.socket.getaddrinfo", lambda *a, **k: infos)


def configure(monkeypatch, dev_http, hosts):
    if dev_http:
        monkeypatch.setenv("PAYBROKER_DEV_HTTP", "1")
    if hosts is not None:
        monkeypatch.setenv("PAYBROKER_DEV_WEBHOOK_HOSTS", hosts)


URL = "http://shopfront:8104/webhooks/paybroker"


@pytest.mark.parametrize("dev_http,hosts", [(False, None), (False, "shopfront"), (True, None), (True, ""), (True, "other,another.localhost")])
def test_refused_unless_both_switches_are_on_and_the_host_is_listed(monkeypatch, dev_http, hosts):
    configure(monkeypatch, dev_http, hosts)
    resolving(monkeypatch, PRIVATE)
    with pytest.raises(netguard.UnsafeURL):
        netguard.check_webhook_url(URL)
    with pytest.raises(netguard.UnsafeURL):
        netguard.resolve_webhook_url(URL)


def test_listed_host_is_accepted_over_http_and_pinned(monkeypatch):
    configure(monkeypatch, True, "shopfront, shopfront.localhost")
    resolving(monkeypatch, PRIVATE)
    assert netguard.check_webhook_url(URL) == "http://shopfront:8104/webhooks/paybroker"
    url, headers, _ = netguard.resolve_webhook_url(URL).request_args()
    assert url.startswith("http://172.18.0.5:8104/") and headers["Host"] == "shopfront:8104"
    resolving(monkeypatch, [(2, 1, 6, "", ("127.0.0.1", 8104))])
    assert netguard.check_webhook_url("http://Shopfront.Localhost:8104/x") == "http://Shopfront.Localhost:8104/x"


def test_ip_literal_can_be_listed(monkeypatch):
    configure(monkeypatch, True, "172.18.0.5")
    resolving(monkeypatch, PRIVATE)
    assert netguard.check_webhook_url("http://172.18.0.5:8104/hook").endswith("/hook")


def test_unlisted_private_host_stays_refused(monkeypatch):
    configure(monkeypatch, True, "shopfront")
    resolving(monkeypatch, PRIVATE)
    for url in ("http://intranet:8104/x", "http://shopfront.evil.com/x", "http://xshopfront/x", "http://10.0.0.5/x"):
        with pytest.raises(netguard.UnsafeURL, match="private or local"):
            netguard.check_webhook_url(url)


@pytest.mark.parametrize("host", ["shopfront", "169.254.169.254"])
def test_link_local_is_refused_even_when_listed(monkeypatch, host):
    configure(monkeypatch, True, "shopfront,169.254.169.254")
    resolving(monkeypatch, LINK_LOCAL)
    with pytest.raises(netguard.UnsafeURL, match="link-local"):
        netguard.check_webhook_url(f"http://{host}/latest/meta-data")


def test_ipv6_link_local_is_refused_even_when_listed(monkeypatch):
    configure(monkeypatch, True, "fe80::1")
    resolving(monkeypatch, [(10, 1, 6, "", ("fe80::1", 80, 0, 0))])
    with pytest.raises(netguard.UnsafeURL, match="link-local"):
        netguard.check_webhook_url("http://[fe80::1]/x")


def test_mixed_answer_with_a_link_local_address_is_refused(monkeypatch):
    configure(monkeypatch, True, "shopfront")
    resolving(monkeypatch, PRIVATE + LINK_LOCAL)
    with pytest.raises(netguard.UnsafeURL, match="link-local"):
        netguard.check_webhook_url(URL)


def test_rebinding_to_link_local_after_listing_is_refused_at_delivery(monkeypatch):
    """Delivery resolves again and pins what it checked. A listed name that starts answering with a metadata address is refused. A listed name
    that moves to ANOTHER private address is accepted: the allowlist trusts the name (a dev stack's DNS is the container network's), and
    pinning only closes the gap between this check and the connection."""
    configure(monkeypatch, True, "shopfront")
    resolving(monkeypatch, PRIVATE)
    assert netguard.resolve_webhook_url(URL).ip == "172.18.0.5"
    resolving(monkeypatch, LINK_LOCAL)
    with pytest.raises(netguard.UnsafeURL, match="link-local"):
        netguard.resolve_webhook_url(URL)
    resolving(monkeypatch, [(2, 1, 6, "", ("10.9.9.9", 8104))])
    assert netguard.resolve_webhook_url(URL).ip == "10.9.9.9"      # the stated limit


@pytest.mark.parametrize("hosts", ["*", "*.localhost", "10.0.0.0/8", "172.18.0.0/16", "shopfront:8104", "http://shopfront", "shop front", "a/b", "-x"])
def test_no_wildcards_ranges_ports_or_urls(monkeypatch, hosts):
    configure(monkeypatch, True, hosts)
    assert netguard.dev_webhook_hosts() == ()
    resolving(monkeypatch, PRIVATE)
    with pytest.raises(netguard.UnsafeURL):
        netguard.check_webhook_url(URL)


def test_hosts_are_normalized_and_only_active_with_dev_http(monkeypatch):
    configure(monkeypatch, False, "ShopFront")
    assert netguard.dev_webhook_hosts() == ()
    configure(monkeypatch, True, "ShopFront, ,shopfront,*.x")
    assert netguard.dev_webhook_hosts() == ("shopfront",)


def test_public_hosts_and_other_checks_are_unchanged(monkeypatch):
    configure(monkeypatch, True, "shopfront")
    resolving(monkeypatch, PUBLIC)
    assert netguard.check_webhook_url("https://shop.example.com/hook") == "https://shop.example.com/hook"
    with pytest.raises(netguard.UnsafeURL, match="plain http"):
        netguard.check_webhook_url("http://shop.example.com/hook")
    resolving(monkeypatch, PRIVATE)
    for bad in ("http://user:pw@shopfront/x", "http://shopfront/x?a=1", "ftp://shopfront/x"):
        with pytest.raises(netguard.UnsafeURL):
            netguard.check_webhook_url(bad)


def test_the_allowlist_never_applies_to_llm_provider_urls(monkeypatch):
    configure(monkeypatch, True, "shopfront")
    resolving(monkeypatch, PRIVATE)
    with pytest.raises(netguard.UnsafeURL, match="private or local"):
        netguard.check_url("http://shopfront:8104/v1")


def test_allow_private_urls_is_not_needed_and_not_implied(monkeypatch):
    configure(monkeypatch, True, "shopfront")
    resolving(monkeypatch, PRIVATE)
    netguard.check_webhook_url(URL)
    with pytest.raises(netguard.UnsafeURL):
        netguard.check_webhook_url("http://other:8104/x")


@needs_db
def test_readyz_lists_the_hosts_and_startup_warns(client, monkeypatch, caplog):
    assert client.get("/readyz").json()["dev_webhook_hosts"] == []
    monkeypatch.setenv("PAYBROKER_DEV_WEBHOOK_HOSTS", "shopfront")
    assert client.get("/readyz").json()["dev_webhook_hosts"] == []            # DEV_HTTP is off
    monkeypatch.setenv("PAYBROKER_DEV_HTTP", "1")
    assert client.get("/readyz").json()["dev_webhook_hosts"] == ["shopfront"]
    from paybroker import api
    caplog.set_level(logging.WARNING, logger="paybroker")
    api._warn_dev_webhook_hosts()
    assert any("PAYBROKER_DEV_WEBHOOK_HOSTS is ON (shopfront)" in r.getMessage() for r in caplog.records)
    caplog.clear()
    monkeypatch.delenv("PAYBROKER_DEV_HTTP")
    api._warn_dev_webhook_hosts()
    assert not caplog.records
