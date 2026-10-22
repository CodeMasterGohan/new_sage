"""Admin API auth and upload queue contract."""

from __future__ import annotations

from fastapi.testclient import TestClient

from services.manual_import import api


class FakeStorage:
    objects = {}

    def put(self, key, data, mime):
        self.objects[key] = data


class FakeJob:
    id = "test-rq-job"


class FakeQueue:
    def __init__(self, *args, **kwargs):
        pass

    def enqueue(self, function, *args, **kwargs):
        assert function == "services.manual_import.jobs.process_import"
        return FakeJob()


def test_admin_auth_upload(monkeypatch):
    token = "a" * 40
    monkeypatch.setenv("SAGE_ADMIN_TOKEN", token)
    monkeypatch.setattr(api, "ImportStorage", FakeStorage)
    monkeypatch.setattr(api, "Queue", FakeQueue)
    monkeypatch.setattr(api, "queue_connection", lambda: None)
    client = TestClient(api.app)
    request = dict(
        data={"library": "gitlab", "version": "18.4"},
        files={"files": ("guide.md", b"# GitLab guide", "text/markdown")},
    )
    assert client.post("/admin/imports", **request).status_code == 401
    assert (
        client.post(
            "/admin/imports", headers={"Authorization": "Bearer wrong"}, **request
        ).status_code
        == 401
    )
    resp = client.post("/admin/imports", headers={"Authorization": f"Bearer {token}"}, **request)
    assert resp.status_code == 202, resp.text
    assert resp.json()["status_url"] == "/admin/imports/test-rq-job"
    assert any(key.startswith("manual-imports/") for key in FakeStorage.objects)


def test_api_fails_closed_without_admin_token(monkeypatch):
    monkeypatch.delenv("SAGE_ADMIN_TOKEN", raising=False)
    client = TestClient(api.app)
    resp = client.post(
        "/admin/imports",
        data={"library": "gitlab", "version": "18.x"},
        files={"files": ("readme.md", b"hello")},
    )
    assert resp.status_code == 503
