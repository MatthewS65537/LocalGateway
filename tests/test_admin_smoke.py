from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from localgateway import config as config_mod
from localgateway.auth import client_host as real_client_host
from localgateway.supervisor import create_supervisor_app
from localgateway.supervisor import Supervisor


@pytest.fixture()
def supervisor_app(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"server": {"api_key": null}, "providers": [], "models": []}')
    config_mod._config = None
    config_mod._config_mtime = -1.0
    config_mod.set_config_path(str(cfg_path))

    sup = Supervisor(str(cfg_path), "127.0.0.1", 3456, 4456)
    return create_supervisor_app(sup)


@pytest.fixture()
def page_client(supervisor_app, monkeypatch):
    from localgateway import auth as auth_mod

    monkeypatch.setattr(auth_mod, "client_host", lambda req: "127.0.0.1")
    with TestClient(supervisor_app) as c:
        yield c


PAGES = ["/", "/models", "/providers", "/usage", "/logs", "/settings", "/compare/foo,bar"]


@pytest.mark.parametrize("path", PAGES)
def test_pages_render(page_client, path):
    r = page_client.get(path)
    assert r.status_code == 200, path
    assert "LocalGateway" in r.text


def test_model_detail_page(page_client):
    r = page_client.get("/models/some-model")
    assert r.status_code == 200


def test_pages_reference_existing_static_assets(page_client):
    r = page_client.get("/")
    html = r.text
    # Collect /static/* references from script/link tags.
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    assert refs, "expected static asset references on the dashboard page"
    for ref in refs:
        asset = page_client.get(ref)
        assert asset.status_code == 200, f"missing asset: {ref}"


def test_no_inline_event_handlers(page_client):
    """CSP safety: no page may ship inline onclick/onchange/oninput handlers."""
    for path in PAGES:
        r = page_client.get(path)
        assert r.status_code == 200
        assert 'onclick="' not in r.text, path
        assert 'onchange="' not in r.text, path
        assert 'oninput="' not in r.text, path
        # No inline <script> blocks either (theme init is an external file).
        assert "<script>" not in r.text.replace("</script>", ""), path


def test_csp_header_on_static_and_pages(page_client):
    for path in PAGES + ["/static/app.css"]:
        r = page_client.get(path)
        assert r.status_code == 200
        csp = r.headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp
