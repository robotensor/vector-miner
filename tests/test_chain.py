from __future__ import annotations

import httpx
import pytest

from vector_miner import chain


D = "sha256:" + "a" * 64


def _registry(monkeypatch, handler):
    real = httpx.Client

    def client(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)
    monkeypatch.setattr(chain.httpx, "Client", client)


def test_image_exists_with_anonymous_token(monkeypatch):
    seen = []

    def h(req: httpx.Request):
        seen.append(req.url.path)
        if req.url.path == "/token":
            assert req.url.params["scope"] == "repository:alice/x:pull"
            return httpx.Response(200, json={"token": "t"})
        if req.headers.get("authorization") != "Bearer t":
            return httpx.Response(401, headers={"www-authenticate":
                                                'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:alice/x:pull"'})
        return httpx.Response(200, headers={"docker-content-digest": D})
    _registry(monkeypatch, h)
    assert chain.image_exists(f"ghcr.io/alice/x@{D}") == (True, "")
    assert seen == [f"/v2/alice/x/manifests/{D}", "/token", f"/v2/alice/x/manifests/{D}"]


@pytest.mark.parametrize("status,digest,why", [(404, None, "no such image"), (500, None, "HTTP 500"),
                                               (200, "sha256:" + "b" * 64, "another digest")])
def test_image_exists_refusals(monkeypatch, status, digest, why):
    _registry(monkeypatch, lambda req: httpx.Response(status, headers={"docker-content-digest": digest} if digest else {}))
    ok, msg = chain.image_exists(f"ghcr.io/alice/x@{D}")
    assert not ok and why in msg


def test_private_image_is_refused(monkeypatch):
    def h(req):
        if req.url.path == "/token":
            return httpx.Response(403)
        return httpx.Response(401, headers={"www-authenticate": 'Bearer realm="https://ghcr.io/token",service="ghcr.io"'})
    _registry(monkeypatch, h)
    ok, msg = chain.image_exists(f"ghcr.io/alice/x@{D}")
    assert not ok and "private" in msg


def _private_registry(req):
    if req.url.path == "/token":
        return httpx.Response(401)
    return httpx.Response(401, headers={"www-authenticate": 'Bearer realm="https://ghcr.io/token",service="ghcr.io"'})


def test_image_access_public_private_missing(monkeypatch):
    pulls = []
    monkeypatch.setattr(chain, "own_pull", lambda ref: pulls.append(ref) or ref.startswith("ghcr.io/alice/mine"))
    mode = {"h": lambda req: httpx.Response(200, headers={"docker-content-digest": D})}
    _registry(monkeypatch, lambda req: mode["h"](req))
    assert chain.image_access(f"ghcr.io/alice/x@{D}") == ("public", "") and pulls == []
    mode["h"] = _private_registry
    assert chain.image_access(f"ghcr.io/alice/mine@{D}") == ("private", chain.ANON_REFUSED)
    state, why = chain.image_access(f"ghcr.io/alice/other@{D}")
    assert state == "missing" and "private, or there is no such image" in why
    mode["h"] = lambda req: httpx.Response(500)
    assert chain.image_access(f"ghcr.io/alice/mine@{D}")[0] == "missing"


def test_own_pull_uses_docker_manifest_inspect(monkeypatch):
    import subprocess
    seen = []

    class P:
        returncode = 0
    monkeypatch.setattr(chain.subprocess, "run", lambda argv, **k: seen.append(argv) or P())
    assert chain.own_pull(f"ghcr.io/alice/x@{D}") and seen == [["docker", "manifest", "inspect", f"ghcr.io/alice/x@{D}"]]
    P.returncode = 1
    assert not chain.own_pull(f"ghcr.io/alice/x@{D}")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("docker", 1)
    monkeypatch.setattr(chain.subprocess, "run", boom)
    assert not chain.own_pull(f"ghcr.io/alice/x@{D}")
