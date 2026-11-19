"""The demo server wires the public API, the guides and the portal."""

from __future__ import annotations

import importlib.util
import os
import sys

from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, "demo")


def _load_demo():
    sys.path.insert(0, DEMO)
    try:
        spec = importlib.util.spec_from_file_location("qcli_demo_main", os.path.join(DEMO, "main.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(DEMO)


def test_demo_serves_portal_docs_and_api():
    result = _load_demo().create_demo_server()
    with TestClient(result.app) as client:
        doc = client.get("/qcli/api/openapi.json").json()
        assert list(doc["paths"]) == ["/v1/demo/time"]
        assert doc["paths"]["/v1/demo/time"]["get"]["x-required-permission"] == "demo:read"
        assert "index" in [p["slug"] for p in client.get("/qcli/docs/pages").json()]
        assert client.get("/v1/demo/time").status_code == 401
        portal = result.demo_portal_plugin
        if portal.site_dir:
            body = client.get("/qcli/portal/portal.json").json()
            assert body["docs"]["pages"] == "/qcli/docs/pages"
            assert body["api"]["openapi"] == "/qcli/api/openapi.json"
            assert client.get("/qcli/portal/").status_code == 200
