from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import httpx
import pytest
from bittensor_wallet import Keypair

from vector_miner import api as api_mod
from vector_miner import chain, cli, protocol

REF = "ghcr.io/alice/bin-expert@sha256:" + "a" * 64


D = "sha256:" + "a" * 64


def test_image_rules_match_the_subnet():
    assert protocol.REGISTRIES == ("ghcr.io",) and protocol.MAX_LEN == 128
    img = protocol.parse_image(f"ghcr.io/alice/bin-expert@{D}")
    assert img == {"ref": f"ghcr.io/alice/bin-expert@{D}", "host": "ghcr.io", "repo": "alice/bin-expert", "digest": D}
    assert protocol.parse_image(f"  ghcr.io/a/b/c@{D} ")["ref"] == f"ghcr.io/a/b/c@{D}"
    for bad in ("ghcr.io/alice/bin-expert:latest", "ghcr.io/alice/bin-expert", f"alice/bin-expert@{D}",
                f"ghcr.io/Alice/x@{D}", "ghcr.io/a/x@sha256:abc", f"ghcr.io/a/x:v1@{D}", f"ghcr.io/a/x@sha256:{'A' * 64}",
                f"ghcr.io/a//x@{D}", f"ghcr.io/a/x-@{D}", ""):
        with pytest.raises(protocol.BadImage):
            protocol.parse_image(bad)
    for host in ("docker.io", "ghcr.io:443", "GHCR.io", "quay.io"):
        with pytest.raises(protocol.BadImage):
            protocol.parse_image(f"{host}/alice/x@{D}")
    with pytest.raises(protocol.BadImage, match="not accepted"):
        protocol.parse_image(f"docker.io/alice/x@{D}")
    fill = 128 - len(f"ghcr.io//x@{D}")
    assert len(protocol.parse_image(f"ghcr.io/{'a' * fill}/x@{D}")["ref"]) == 128
    with pytest.raises(protocol.BadImage, match="longer than 128"):
        protocol.parse_image(f"ghcr.io/{'a' * (fill + 1)}/x@{D}")


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
    assert not ok and "public" in msg


def test_commit_sends_the_checked_reference(monkeypatch, capsys):
    sent = {}
    monkeypatch.setattr(cli, "_keypair", lambda a: Keypair.create_from_uri("//Alice"))
    monkeypatch.setattr(api_mod.Api, "dev_commit", lambda self, image: sent.setdefault("image", image) and {"block": 1})
    assert cli.main(["commit", "--image", f"  {REF} ", "--no-check", "--dev-api", "http://api"]) == 0
    assert sent["image"] == REF
    with pytest.raises(SystemExit, match="bad image reference"):
        cli.main(["commit", "--image", f"docker.io/alice/x@{D}", "--no-check", "--dev-api", "http://api"])


def test_signed_request():
    kp = Keypair.create_from_uri("//Alice")
    body = b'{"x":1}'
    h = protocol.sign_headers(kp, "post", "/v1/expert/work", body, nonce="1700000000000")
    msg = f"vector-v1\nPOST\n/v1/expert/work\n{hashlib.sha256(body).hexdigest()}\n1700000000000".encode()
    assert protocol.message("POST", "/v1/expert/work", body, "1700000000000") == msg
    assert h["x-vector-hotkey"] == kp.ss58_address and h["x-vector-nonce"] == "1700000000000"
    assert kp.verify(msg, bytes.fromhex(h["x-vector-signature"]))


def test_commitment_payload():
    info = protocol.commitment_info(REF)
    n = len(REF.encode())
    assert info == {"fields": [{f"Raw{n}": "0x" + REF.encode().hex()}]}
    with pytest.raises(protocol.BadImage):
        protocol.commitment_info("x" * 129)
    bt = pytest.importorskip("bittensor")
    if not hasattr(bt, "intents"):
        pytest.skip("bittensor < 11")
    from vector_miner.chain import commit_call
    c = commit_call(7, REF)
    assert (c.module, c.function) == ("Commitments", "set_commitment") and c.params == {"netuid": 7, "info": info}


HERE = Path(__file__).resolve().parent
HOT = Keypair.create_from_uri("//Alice").ss58_address


def test_status_reads_the_public_board(monkeypatch, capsys):
    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/v1/skills/bin-picking/expert":
            return httpx.Response(200, json={"king": {"hotkey": "5King", "score": 0.61}, "current": {"n": 4, "status": "open"},
                                             "rounds": [{"n": 3}, {"n": 2}]})
        if p.startswith("/v1/skills/bin-picking/expert/rounds/"):
            n = int(p.rsplit("/", 1)[1])
            return httpx.Response(200, json={"round": {"n": n}, "submissions": [
                {"rank": 1, "hotkey": HOT, "score": 0.5 + n / 100, "status": "scored"}, {"rank": 2, "hotkey": "5Other"}]})
        return httpx.Response(404)

    monkeypatch.setattr(api_mod, "make_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(handler)))
    assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [s["round"] for s in out["submissions"]] == [4, 3, 2] and out["king"] is False and out["king_score"] == 0.61


def test_work_is_gone():
    with pytest.raises(SystemExit):
        cli.main(["work", "--api", "http://api"])


def test_eval_runs_the_local_harness(capsys, tmp_path):
    pytest.importorskip("vector_env")
    from vector_env import assets, harness, scenes
    from vector_env.settings import env_config
    try:
        assets.ensure()
    except Exception as e:
        pytest.skip(f"no asset bundle: {e}")
    cmd = f"node {HERE.parent / 'example' / 'solve.mjs'}"
    rows = tmp_path / "results.jsonl"
    wall = 20 * float(env_config()["harness"]["wall_s"])
    assert cli.main(["eval", "--command", cmd, "--scenes", "2", "--seed", "1", "--workers", "2", "--wall", str(wall),
                     "--results", str(rows)]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    res = [json.loads(x) for x in rows.read_text().splitlines()]
    assert out["scenes"] == 2 and out["wall_s"] == wall and out["successes"] == sum(r["success"] for r in res), out
    assert out["score"] == out["successes"] / 2 and out["successes"] >= 1, out
    for i, r in enumerate(res):
        assert r["wall_s"] < wall and r["outcome"] != "wall_timeout", r
        sc = scenes.generate(scenes.seed_for(1, i))
        rp = harness.replay(sc, r["ctrl"], r["n"], time_limit_s=out["time_limit_s"])
        assert (rp["outcome"], rp["success"], rp["t"]) == (r["outcome"], r["success"], r["t"])
