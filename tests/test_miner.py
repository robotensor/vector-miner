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
    assert not ok and "private" in msg


def _api(monkeypatch, handler):
    monkeypatch.setattr(api_mod, "make_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(handler)))


def _subs(rows):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/skills/bin-picking/expert/submissions":
            return httpx.Response(200, json={"submissions": [r for r in rows if r["hotkey"] == req.url.params["hotkey"]]})
        return httpx.Response(404, json={"detail": "no"})
    return handler


def test_commit_sends_the_checked_reference(monkeypatch, capsys):
    sent = {}
    monkeypatch.setattr(cli, "_keypair", lambda a: Keypair.create_from_uri("//Alice"))
    monkeypatch.setattr(api_mod.Api, "dev_commit", lambda self, image: sent.setdefault("image", image) and {"block": 1})
    _api(monkeypatch, _subs([{"hotkey": "5Other", "id": "s0", "status": "phase1"}]))
    assert cli.main(["commit", "--image", f"  {REF} ", "--no-check", "--dev-api", "http://api"]) == 0
    assert sent["image"] == REF
    assert f"vector-miner serve --image {REF} --api http://api;" in capsys.readouterr().err
    assert cli.main(["commit", "--image", REF, "--no-check", "--dev-api", "http://api", "--wallet.name", "cold",
                     "--wallet.hotkey", "hk1", "--wallet.path", "/w"]) == 0
    err = capsys.readouterr().err
    assert (f"vector-miner serve --image {REF} --api http://api --wallet.name cold --wallet.hotkey hk1 "
            "--wallet.path /w;") in err
    assert "vector-miner status --api http://api --wallet.name cold --wallet.hotkey hk1 --wallet.path /w" in err
    with pytest.raises(SystemExit, match="bad image reference"):
        cli.main(["commit", "--image", f"docker.io/alice/x@{D}", "--no-check", "--dev-api", "http://api"])


@pytest.mark.parametrize("status", ["waiting_connection", "phase1", "review", "below_king", "crowned"])
def test_commit_refuses_a_hotkey_that_already_has_a_submission(monkeypatch, status):
    alice = Keypair.create_from_uri("//Alice")
    monkeypatch.setattr(cli, "_keypair", lambda a: alice)
    monkeypatch.setattr(api_mod.Api, "dev_commit", lambda self, image: pytest.fail("committed"))
    monkeypatch.setattr(chain, "commit", lambda *a: pytest.fail("committed on chain"))
    rows = [{"hotkey": alice.ss58_address, "id": "sub-7", "image": REF, "status": status,
             "committed_at": "2026-11-23T01:00:00+00:00"},
            {"hotkey": alice.ss58_address, "id": "sub-9", "image": REF, "status": "duplicate_hotkey"}]
    _api(monkeypatch, _subs(rows))
    for argv in (["--dev-api", "http://api"], ["--netuid", "1", "--api", "http://api"]):
        with pytest.raises(SystemExit) as e:
            cli.main(["commit", "--image", REF, "--no-check", *argv])
        msg = str(e.value.code)
        assert f"already has submission sub-7 ({REF}, status {status}" in msg
        assert "one submission per hotkey, ever" in msg and "duplicate_hotkey" in msg and "new hotkey" in msg
        assert (f"a submission in {status} uses it (only invalid, copy, duplicate_hotkey and abandoned commits leave "
                "it free)") in msg


def test_commit_goes_ahead_when_no_earlier_commit_used_the_slot(monkeypatch):
    alice = Keypair.create_from_uri("//Alice")
    sent = []
    monkeypatch.setattr(cli, "_keypair", lambda a: alice)
    monkeypatch.setattr(api_mod.Api, "dev_commit", lambda self, image: sent.append(image) or {"block": 9})
    rows = [{"hotkey": alice.ss58_address, "id": f"sub-{k}", "image": REF, "status": st, "commit_block": k}
            for k, st in enumerate(("invalid", "copy", "duplicate_hotkey", "abandoned"))]
    _api(monkeypatch, _subs(rows))
    assert cli.main(["commit", "--image", REF, "--no-check", "--dev-api", "http://api"]) == 0 and sent == [REF]
    rows += [{"hotkey": alice.ss58_address, "id": "sub-5", "image": REF, "status": "below_king", "commit_block": 5}]
    with pytest.raises(SystemExit, match="already has submission sub-5 .*status below_king"):
        cli.main(["commit", "--image", REF, "--no-check", "--dev-api", "http://api"])
    assert sent == [REF]


def test_commit_needs_the_api_check_or_its_opt_out(monkeypatch):
    alice = Keypair.create_from_uri("//Alice")
    monkeypatch.setattr(cli, "_keypair", lambda a: alice)
    monkeypatch.delenv("VECTOR_MINER_API", raising=False)
    with pytest.raises(SystemExit, match="--api .* is required: commit first checks"):
        cli.main(["commit", "--image", REF, "--no-check", "--netuid", "1"])

    def down(req):
        raise httpx.ConnectError("refused")
    _api(monkeypatch, down)
    with pytest.raises(SystemExit, match="cannot reach http://api .*--no-api-check"):
        cli.main(["commit", "--image", REF, "--no-check", "--netuid", "1", "--api", "http://api"])
    sent = []
    monkeypatch.setattr(chain, "commit", lambda net, netuid, kp, ref: sent.append((net, netuid, ref)) or {"ok": True})
    assert cli.main(["commit", "--image", REF, "--no-check", "--netuid", "1", "--no-api-check", "--network", "test"]) == 0
    assert sent == [("test", 1, REF)]


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


def test_status_shows_progress_queue_phase2_king_and_verdict(monkeypatch, capsys):
    rows = {HOT: [{"id": "sub-3", "hotkey": HOT, "image": REF, "status": "scored", "committed_at": "2026-11-23T01:00:00Z",
                   "salt_sha256": "c" * 64, "phase1": {"done": 1000, "total": 1000, "score": 0.71}, "queue_position": 2}]}
    king = {"hotkey": "5King", "best_score": 0.64}

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/v1/skills/bin-picking/expert":
            return httpx.Response(200, json={"king": king, "margin": 0.02})
        if p == "/v1/skills/bin-picking/expert/submissions":
            return httpx.Response(200, json={"submissions": rows.get(req.url.params["hotkey"], [])})
        return httpx.Response(404)

    _api(monkeypatch, handler)
    assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    s = out["submissions"][0]
    assert out["king"] is False and out["king_best"] == 0.64 and out["margin"] == 0.02
    assert s["phase1"] == {"done": 1000, "total": 1000, "score": 0.71, "successes": None} and s["queue_position"] == 2
    assert "evaluation queue position 2" in s["verdict"] and "sub-3: phase 1 done: 71.0 %" in cap.err
    rows[HOT][0].update(round=3, evaluates_at="2026-11-30T00:00:00+00:00")
    assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 0
    s = json.loads(capsys.readouterr().out)["submissions"][0]
    assert s["round"] == 3 and "round 3 ends at 2026-11-30T00:00:00+00:00, then its evaluation" in s["verdict"]
    assert out["king_image"] is None and out["king_note"] == "king: no image published"
    assert "vector-miner: king: no image published" in cap.err
    views = {
        "waiting_connection": ({}, "run vector-miner serve"),
        "phase1": ({"phase1": {"done": 412, "total": 1000}}, "412/1000 scenes"),
        "below_king": ({"phase1": {"score": 0.6}}, "not above the king's best 64.0 %"),
        "phase2": ({}, "every action hash must equal phase 1's"),
        "king_run": ({"king_run": {"done": 300, "total": 1000}}, "king's image is running on your scenes (300/1000)"),
        "crowned": ({"phase1": {"score": 0.71}, "king_run": {"score": 0.62}}, "crowned: 71.0 % against the king's 62.0 %"),
        "not_crowned": ({"phase1": {"score": 0.71}, "king_run": {"score": 0.7}}, "not crowned"),
        "disqualified": ({"reason": "action hash differs on 3 of 100 scenes"}, "action hash differs on 3 of 100"),
        "abandoned": ({}, "no miner connection within 48 h of the commit; it did not use this hotkey's submission"),
        "invalid": ({"reason": "the image is private"}, "the image is private; it did not use this hotkey's submission"),
        "copy": ({}, "the hotkey may commit again"),
        "review": ({"retry_at": "2026-11-23T16:00:00+00:00"},
                   "re-run because of a problem on our side, not your expert's; it runs again at 2026-11-23T16:00:00+00:00; "
                   "keep vector-miner serve running"),
    }
    for st, (extra, want) in views.items():
        rows[HOT] = [{"id": "sub-3", "hotkey": HOT, "status": st, **extra}]
        assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 0
        s = json.loads(capsys.readouterr().out)["submissions"][0]
        assert want in s["verdict"], (st, s)
        assert s["final"] == (st in ("below_king", "crowned", "not_crowned", "disqualified", "abandoned", "invalid",
                                     "copy"))
        if st == "king_run":
            assert s["king_same_scenes"]["done"] == 300
        if st == "review":
            assert s["retry_at"] == "2026-11-23T16:00:00+00:00"
    for st, extra, want in (("phase2", {"stage": "copy"}, "we copy your committed image into our registry first"),
                            ("king_run", {"stage": "decisive", "decisive": {"done": 3, "total": 7}},
                             "decisive re-check: your image runs in docker on the scenes you won and the king lost (3/7)"),
                            ("disqualified", {"reason": "image no longer available: manifest unknown"},
                             "disqualified: image no longer available: manifest unknown; final")):
        rows[HOT] = [{"id": "sub-3", "hotkey": HOT, "status": st, **extra}]
        assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 0
        s = json.loads(capsys.readouterr().out)["submissions"][0]
        assert want in s["verdict"], (st, s)
    rows[HOT] = [{"id": "sub-3", "hotkey": HOT, "status": "review", "phase2": {"done": 40, "total": 100}}]
    assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 0
    v = json.loads(capsys.readouterr().out)["submissions"][0]["verdict"]
    assert v.endswith("it runs again automatically") and "serve" not in v
    king["hotkey"], king["image"] = HOT, REF
    rows[HOT] = []
    assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["king_image"] == REF and "king_note" not in out
    assert out["king"] is True and out["submissions"] == [] and "vector-miner commit" in out["note"]


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
        sc, _ = scenes.draw_index(1, i)
        rp = harness.replay(sc, r["ctrl"], r["n"], time_limit_s=out["time_limit_s"], result=r)
        assert (rp["outcome"], rp["success"], rp["t"]) == (r["outcome"], r["success"], r["t"])


def test_status_and_commit_without_a_wallet(tmp_path):
    for argv in (["status", "--api", "http://api"], ["commit", "--image", REF, "--no-check", "--netuid", "1"]):
        with pytest.raises(SystemExit) as e:
            cli.main([*argv, "--wallet.path", str(tmp_path), "--wallet.name", "miner"])
        msg = str(e.value.code)
        assert msg.startswith("vector-miner: no wallet hotkey miner/default in ")
        assert f".venv/bin/btcli wallet new-coldkey --wallet miner --wallet-path {tmp_path}, then " in msg
        assert msg.endswith(f".venv/bin/btcli wallet new-hotkey --wallet miner --wallet-hotkey default --wallet-path {tmp_path}")
        assert "new_" not in msg and "--wallet.name/--wallet.hotkey" in msg
        assert ("--hotkey <ss58>" in msg) == (argv[0] == "status")
    (tmp_path / "miner").mkdir()
    (tmp_path / "miner" / "coldkeypub.txt").write_text("{}")
    msg = cli.btcli_create("miner", "hk", str(tmp_path))
    assert msg == f".venv/bin/btcli wallet new-hotkey --wallet miner --wallet-hotkey hk --wallet-path {tmp_path}"
    assert cli.btcli_create("nobody-x", "hk", cli.DEFAULT_WALLETS).endswith("new-hotkey --wallet nobody-x --wallet-hotkey hk")


def test_status_unreachable_api_is_one_line(monkeypatch, capsys):
    def handler(req):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(api_mod, "make_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(handler)))
    assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 2
    assert capsys.readouterr().err.startswith("vector-miner: cannot reach http://api")


def _no_docker_scenes(monkeypatch):
    from vector_env import cli as env_cli
    scs = [{"scene_id": f"s{i}-0000000{i}", "seed": i, "xml": "<mujoco/>", "meta": {"parts": [1]}, "checkpoint": "",
            "state_hash": str(i) * 8, "target": 1} for i in range(3)]
    monkeypatch.setattr(env_cli, "draw_scenes", lambda n, seed, draw=None, log=None, **k: (scs[:n], []))


def test_eval_image_without_docker_is_an_infra_error(monkeypatch, capsys, tmp_path):
    _no_docker_scenes(monkeypatch)
    assert cli.main(["eval", "--image", "ghcr.io/a/b:1", "--docker", str(tmp_path / "none"), "--scenes", "3"]) == 2
    cap = capsys.readouterr()
    assert cap.out == "" and '"score"' not in cap.err
    line = cap.err.strip().splitlines()[-1]
    assert line.startswith("vector-miner: no scene ran here (docker_unavailable") and "--command" in line


def test_eval_pull_without_docker_is_one_line(monkeypatch, capsys, tmp_path):
    _no_docker_scenes(monkeypatch)
    assert cli.main(["eval", "--image", "ghcr.io/a/b:1", "--pull", "--docker", str(tmp_path / "none")]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and err[0].startswith("vector-miner: cannot pull ghcr.io/a/b:1: no docker binary")


def test_eval_skips_like_the_round(monkeypatch, capsys):
    from vector_env import harness, scenes
    from vector_env.compose import SceneError
    bad = {scenes.seed_for(2, 0) + k for k in range(scenes.SEED_TRIES)}

    def gen(seed, draw):
        if seed in bad:
            raise SceneError("no part in the bin is visible enough to be a target")
        return {"scene_id": f"s{seed}", "seed": seed, "meta": {"parts": [1]}}
    monkeypatch.setattr(scenes, "generate", gen)
    async def arun(ex, scs, **k):
        return [{"scene_id": s["scene_id"], "outcome": "success", "success": True, "t": 3.0, "n": 60, "wall_s": 1.0,
                 "infra": False} for s in scs]
    monkeypatch.setattr(harness, "arun", arun)
    assert cli.main(["eval", "--command", "true", "--scenes", "3", "--seed", "2"]) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert out["skipped"] == [0] and out["scenes"] == 2 and out["score"] == 1.0 and "Traceback" not in cap.err


def test_eval_passes_trace_and_scene_cache(monkeypatch, capsys, tmp_path):
    from vector_env import cli as env_cli

    from vector_miner import local
    seen = {}

    def evaluate(expert, n, seed, **k):
        seen.update(k)
        raise env_cli.EvalError("stop here")
    monkeypatch.setattr(local, "evaluate", evaluate)
    assert cli.main(["eval", "--command", "true", "--trace", str(tmp_path / "t"), "--scene-cache", str(tmp_path / "c"),
                     "--stderr", str(tmp_path / "e"), "--workers", "3"]) == 2
    assert seen["trace_dir"] == str(tmp_path / "t") and seen["scene_cache_dir"] == str(tmp_path / "c")
    assert seen["stderr_dir"] == str(tmp_path / "e") and seen["workers"] == 3
    assert capsys.readouterr().err.strip() == "vector-miner: stop here"
    assert cli.main(["eval", "--command", "true", "--no-scene-cache"]) == 2
    assert seen["scene_cache_dir"] is False and seen["trace_dir"] is None
    assert seen["limits"]["first_step_deadline_s"] == 10.0 and seen["limits"]["step_deadline_s"] == 2.0
    assert cli.main(["eval", "--command", "true", "--step-deadline", "0.5", "--first-step-deadline", "3"]) == 2
    assert seen["limits"] == {"first_step_deadline_s": 3.0, "step_deadline_s": 0.5}
    with pytest.raises(SystemExit, match="--step-deadline must be a positive"):
        cli.main(["eval", "--command", "true", "--step-deadline", "0"])


def _bin(d, name, body):
    p = d / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(0o755)


def test_setup_pins_vector_env_and_vector_sim(tmp_path):
    import os
    import re
    import shutil
    import subprocess
    root = HERE.parent
    repo = tmp_path / "vector-miner"
    (repo / "scripts").mkdir(parents=True)
    (repo / "example").mkdir()
    for f in ("scripts/setup.sh", "example/Dockerfile", "pyproject.toml"):
        shutil.copy(root / f, repo / f)
    env_ver = re.search(r'"vector-env==([^"]+)"', (root / "pyproject.toml").read_text()).group(1)
    sim_ref = re.search(r"^ARG VECTOR_SIM_REF=(\S+)", (root / "example" / "Dockerfile").read_text(), re.M).group(1)
    assert (env_ver, sim_ref) == ("0.1.0", "v0.1.0")
    bin_, log = tmp_path / "bin", tmp_path / "calls.log"
    bin_.mkdir()
    fake_env_setup = f'echo "env-setup VECTOR_SIM_REF=$VECTOR_SIM_REF VECTOR_ENV_JS_ONLY=$VECTOR_ENV_JS_ONLY" >> {log}\n'
    _bin(bin_, "git", f'echo "git $@" >> {log}\nif [ "$1" = clone ]; then d="${{@: -1}}"; mkdir -p "$d/vector_env" "$d/scripts"; '
                      f"printf '%s\\n' '#!/usr/bin/env bash' '{fake_env_setup.strip()}' > \"$d/scripts/setup.sh\"; "
                      'chmod +x "$d/scripts/setup.sh"; exit 0; fi\nexit 1\n')
    _bin(bin_, "uv", "exit 0\n")
    _bin(bin_, "npm", "exit 0\n")
    env = {k: v for k, v in os.environ.items() if k not in ("VECTOR_ENV_REF", "VECTOR_SIM_REF", "VECTOR_ENV_DIR")}
    env["PATH"] = f"{bin_}:{env['PATH']}"
    p = subprocess.run(["bash", str(repo / "scripts" / "setup.sh")], env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    calls = log.read_text().splitlines()
    assert calls[0].startswith(f"git clone --depth 1 --branch v{env_ver} ") and calls[0].endswith("../vector-env")
    assert f"env-setup VECTOR_SIM_REF={sim_ref} VECTOR_ENV_JS_ONLY=1" in calls
    assert f"at {sim_ref}" in p.stderr and "assets: not installed yet" in p.stderr


def _det_run(monkeypatch, hashes):
    from vector_env import harness
    _no_docker_scenes(monkeypatch)
    calls = []

    async def arun(ex, scs, **k):
        calls.append([s["scene_id"] for s in scs])
        n = len(calls) - 1
        return [{"scene_id": s["scene_id"], "outcome": hashes[n][s["scene_id"]][1], "success": False, "t": 0.0, "n": 3,
                 "wall_s": 1.0, "infra": hashes[n][s["scene_id"]][1] == "start_failed",
                 "action_hash": hashes[n][s["scene_id"]][0]} for s in scs]
    monkeypatch.setattr(harness, "arun", arun)
    return calls


def test_eval_determinism_runs_twice_and_compares_action_hashes(monkeypatch, capsys):
    ids = [f"s{i}-0000000{i}" for i in range(3)]
    same = {i: ("%016x" % k, "missed") for k, i in enumerate(ids)}
    calls = _det_run(monkeypatch, [same, same])
    assert cli.main(["eval", "--command", "true", "--determinism", "3"]) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out.strip().splitlines()[-1])
    assert calls == [ids, ids[::-1]], "the second run goes in reverse order"
    assert out["determinism"] == {"compared": 3, "equal": 3, "all_equal": True, "mismatched": [], "infra": 0}
    assert "deterministic on 3 scenes" in cap.err
    calls = _det_run(monkeypatch, [same, {**same, ids[1]: ("f" * 16, "missed")}])
    assert cli.main(["eval", "--command", "true", "--determinism", "3"]) == 1
    cap = capsys.readouterr()
    assert json.loads(cap.out.strip().splitlines()[-1])["determinism"]["mismatched"] == [ids[1]]
    assert "NOT deterministic: 1 of 3" in cap.err and "seed every random draw" in cap.err
    _det_run(monkeypatch, [same, {**same, ids[2]: (None, "start_failed")}])
    assert cli.main(["eval", "--command", "true", "--determinism", "3"]) == 2
    assert "not a determinism result" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="at least 1"):
        cli.main(["eval", "--command", "true", "--determinism", "0"])


def test_eval_determinism_on_real_scenes(capsys):
    pytest.importorskip("vector_env")
    from vector_env import assets
    if assets.find() is None:
        pytest.skip(assets.MISSING)
    base = f"{sys.executable} {HERE / 'fake_expert.py'}"
    assert cli.main(["eval", "--command", f"{base} track 5", "--determinism", "2", "--seed", "3", "--workers", "2",
                     "--time-limit", "3"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["determinism"]["all_equal"] and out["determinism"]["compared"] == 2
    assert cli.main(["eval", "--command", f"{base} noisy 5", "--determinism", "2", "--seed", "3", "--workers", "2",
                     "--time-limit", "3"]) == 1
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert len(out["determinism"]["mismatched"]) == 2


def test_the_latency_report_flags_late_acts():
    from vector_miner import local
    limits = {"first_step_deadline_s": 10.0, "step_deadline_s": 2.0}
    timing = {"0.0": {"scene_id": "s0", "acts": [3.1, 0.2, 0.4]}, "1.0": {"scene_id": "s1", "acts": [11.0, 0.1]},
              "2.0": {"scene_id": "s2", "acts": [1.0, 2.5]}, "3.0": {"scene_id": "s3", "acts": [1.0], "pending": 2.4}}
    res = [{"scene_id": "s3", "outcome": "step_timeout"}]
    lat = local.latency(timing, res, limits, {"first_act_s": 10.0, "later_acts_s": 2.0})
    assert lat["first_act_max_s"] == 11.0 and lat["later_act_max_s"] == 2.5 and lat["step_timeouts"] == 1
    assert lat["late_scenes"] == ["s1", "s2", "s3"] and lat["late_acts"] == 3
    assert [(x["scene_id"], x["act"]) for x in lat["late"]] == [("s1", 1), ("s2", 2), ("s3", 2)]
    lines = local.report_lines(lat)
    assert lines[0].startswith("warning: 3 scenes missed a phase-1 deadline") and "s3 act 2 no answer after 2.4 s" in lines[0]
    ok = local.latency({"0.0": {"scene_id": "s0", "acts": [3.1, 0.2]}}, [], limits)
    assert local.report_lines(ok) == ["deadlines met: first act at most 3.1 s (limit 10 s), later acts at most 0.2 s "
                                      "(limit 2 s)"]


def test_eval_enforces_the_phase1_deadlines_on_real_scenes(capsys, tmp_path):
    pytest.importorskip("vector_env")
    from vector_env import assets
    if assets.find() is None:
        pytest.skip(assets.MISSING)
    base = f"{sys.executable} {HERE / 'fake_expert.py'}"
    argv = ["eval", "--seed", "3", "--scenes", "2", "--workers", "2", "--time-limit", "3"]
    assert cli.main([*argv, "--command", f"{base} track 5", "--strict", "--stderr", str(tmp_path / "e")]) == 0
    cap = capsys.readouterr()
    logs = sorted(p.name for p in (tmp_path / "e").iterdir())
    assert len(logs) == 2 and all(x[:5] in ("0000-", "0001-") and x.endswith(".log") for x in logs), logs
    d = json.loads(cap.out.strip().splitlines()[-1])["deadlines"]
    assert d["late_scenes"] == [] and d["first_act_max_s"] < 10 and "deadlines met" in cap.err
    assert cli.main([*argv, "--command", f"{base} lag 5", "--step-deadline", "0.3"]) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out.strip().splitlines()[-1])
    assert out["outcomes"] == {"step_timeout": 2} and out["deadlines"]["step_timeouts"] == 2, out
    assert "missed a phase-1 deadline" in cap.err and "--strict" in cap.err
    assert cli.main([*argv, "--command", f"{base} lag 5", "--step-deadline", "0.3", "--strict"]) == 1


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


def test_commit_warns_for_a_private_image_and_names_the_team_account(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(cli, "_keypair", lambda a: Keypair.create_from_uri("//Alice"))
    monkeypatch.setattr(api_mod.Api, "dev_commit", lambda self, image: sent.append(image) or {"block": 1})
    monkeypatch.setattr(chain, "image_access", lambda ref: ("private", chain.ANON_REFUSED))

    def h(req):
        if req.url.path == "/v1/skills/bin-picking/expert":
            return httpx.Response(200, json={"pull_account": "teamacct", "registries": ["ghcr.io"]})
        return _subs([])(req)
    _api(monkeypatch, h)
    assert cli.main(["commit", "--image", REF, "--dev-api", "http://api"]) == 0 and sent == [REF]
    err = capsys.readouterr().err
    assert "is private" in err and "share the package with it (Read)" in err and "add teamacct, role Read" in err
    _api(monkeypatch, _subs([]))
    assert cli.main(["commit", "--image", REF, "--dev-api", "http://api"]) == 0
    assert "the subnet's team account (`pull_account`" in capsys.readouterr().err
    monkeypatch.setattr(chain, "image_access", lambda ref: ("missing", "no such image at that digest"))
    with pytest.raises(SystemExit, match="not pullable: no such image"):
        cli.main(["commit", "--image", REF, "--dev-api", "http://api"])
    assert len(sent) == 2
