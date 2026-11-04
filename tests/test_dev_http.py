"""PAYBROKER_DEV_HTTP: plain-http browser origins for a local stack (`http://shop.localhost:8104`), and nothing else. Read from configuration only."""
import logging

import pytest

from paybroker import origins
from tests.conftest import needs_db


@pytest.fixture
def dev_on(monkeypatch):
    monkeypatch.setenv("PAYBROKER_DEV_HTTP", "1")


@pytest.fixture
def dev_off(monkeypatch):
    monkeypatch.delenv("PAYBROKER_DEV_HTTP", raising=False)


OFF_REFUSED = ["http://foo.localhost", "http://foo.localhost:8104", "http://[::1]:3000", "http://example.com", "http://10.0.0.5", "https://[::1]"]


@pytest.mark.parametrize("raw", OFF_REFUSED)
def test_flag_off_refuses_what_it_always_refused(dev_off, raw):
    with pytest.raises(origins.OriginError):
        origins.clean_origin(raw)


@pytest.mark.parametrize("raw", ["http://localhost:3000", "http://127.0.0.1:3000", "https://shop.example.com"])
def test_flag_off_keeps_the_old_accepts(dev_off, raw):
    assert origins.clean_origin(raw) == raw


@pytest.mark.parametrize("raw,clean", [
    ("http://localhost", "http://localhost"),
    ("http://foo.localhost:8104", "http://foo.localhost:8104"),
    ("http://Shop.Localhost", "http://shop.localhost"),
    ("http://127.0.0.1:3000", "http://127.0.0.1:3000"),
    ("http://[::1]:3000", "http://[::1]:3000"),
    ("http://[::1]", "http://[::1]"),
    ("https://shop.example.com", "https://shop.example.com"),
])
def test_flag_on_accepts_loopback_names_over_http(dev_on, raw, clean):
    assert origins.clean_origin(raw) == clean


@pytest.mark.parametrize("raw", [
    "http://example.com", "http://10.0.0.5", "http://192.168.1.2:8000", "http://foo.localhost.evil.com", "http://localhost.evil.com",
    "http://evil-localhost", "http://.localhost", "http://foo.localhost.", "http://*.localhost", "http://[fe80::1]", "http://[::ffff:10.0.0.5]",
    "https://10.0.0.5", "http://foo.localhost/path", "http://user@foo.localhost",
])
def test_flag_on_still_refuses_everything_else(dev_on, raw):
    with pytest.raises(origins.OriginError):
        origins.clean_origin(raw)


@pytest.mark.parametrize("value,on", [("1", True), ("true", True), ("on", True), ("0", False), ("", False), ("no", False), ("localhost", False)])
def test_flag_values(monkeypatch, value, on):
    monkeypatch.setenv("PAYBROKER_DEV_HTTP", value)
    assert origins.dev_http() is on


def test_return_url_follows_the_same_rule(dev_on):
    assert origins.clean_return_url("http://shop.localhost:8104/done", ["http://shop.localhost:8104"]) == "http://shop.localhost:8104/done"
    with pytest.raises(origins.OriginError):
        origins.clean_return_url("http://evil.example.com/done", ["http://shop.localhost:8104"])


@needs_db
def test_readyz_reports_the_relaxation_and_startup_warns(client, monkeypatch, caplog):
    monkeypatch.delenv("PAYBROKER_DEV_HTTP", raising=False)
    assert client.get("/readyz").json()["insecure_transport_relaxed"] is False
    monkeypatch.setenv("PAYBROKER_DEV_HTTP", "1")
    assert client.get("/readyz").json()["insecure_transport_relaxed"] is True
    from paybroker import api
    caplog.set_level(logging.WARNING, logger="paybroker")
    api._warn_dev_http()                                      # what the lifespan calls at start-up
    assert any("PAYBROKER_DEV_HTTP is ON" in r.getMessage() for r in caplog.records)
    caplog.clear()
    monkeypatch.delenv("PAYBROKER_DEV_HTTP")
    api._warn_dev_http()
    assert not caplog.records
