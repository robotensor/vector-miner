from __future__ import annotations

import copy
import hashlib
import json
import statistics
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from vector_miner import api as api_mod
from vector_miner import cli, verify

HERE = Path(__file__).resolve().parent
SALT = "5e" * 16
IMG = "ghcr.io/alice/bin-expert@sha256:" + "a" * 64
IMG_B = "ghcr.io/bob/bin-expert@sha256:" + "b" * 64
ENV = {"version": "0.1.0", "assets_version": "2026.11.1", "assets_sha256": "e" * 64}


def _h(*parts) -> float:
    return int.from_bytes(hashlib.sha256(":".join(map(str, parts)).encode()).digest()[:6], "big") / float(1 << 48)


class Scenes:
    def __init__(self, broken=(), env=None):
        self.broken, self.env, self.calls = set(broken), dict(env or ENV), []

    def generate(self, seed, draw=None):
        self.calls.append(seed)
        if seed in self.broken:
            raise ValueError("could not place the drawn parts")
        sh = hashlib.sha256(f"state:{seed}:{json.dumps(draw)}".encode()).hexdigest()[:16]
        return {"scene_id": f"s{seed}-{sh[:8]}", "seed": int(seed), "state_hash": sh, "meta": {"parts": [1, 2, 3]}}

    def env_info(self):
        return dict(self.env)


class Harness:
    rates = {IMG: 0.8, IMG_B: 0.5}

    def rate(self, expert):
        if expert.get("command"):
            c = expert["command"]
            return float(c[c.index("rate") + 1])
        return self.rates.get(expert.get("image"), 0.0)

    def run(self, expert, scenes, *, time_limit_s, wall_s, workers=1, docker=None, on_result=None):
        out = []
        for sc in scenes:
            ok = _h("scene", sc["seed"]) < self.rate(expert)
            r = {"scene_id": sc["scene_id"], "success": ok, "outcome": "success" if ok else "missed",
                 "t": round(4 + 8 * _h("t", sc["seed"]), 3) if ok else 0.0}
            out.append(r)
            if on_result:
                on_result(len(out) - 1, r)
        return out


def fake_env(**kw) -> SimpleNamespace:
    return SimpleNamespace(scenes=Scenes(**kw), harness=Harness(), wall_s=45.0)


def make_doc(e, n=4, count=12, draw=None, specs=((None, "king", 0.3), (IMG, "challenger", None), (IMG_B, "challenger", None))):
    rows, made = [], []
    for i in range(count):
        for k in range(6):
            try:
                sc = e.scenes.generate(verify.base_seed(SALT, n, i) + k, draw)
                break
            except Exception:
                continue
        made.append(sc)
        rows.append({"index": i, "seed": sc["seed"], "state_hash": sc["state_hash"], "parts": 3})
    images = []
    for b, (ref, role, rate) in enumerate(specs):
        got = e.harness.run({"image": ref} if ref else {"command": ["team", "rate", str(rate)]}, made,
                            time_limit_s=15, wall_s=45)
        succ = [int(r["success"]) for r in got]
        ts = [r["t"] for r in got]
        ok = [t for s, t in zip(succ, ts) if s]
        images.append({"role": role, "rank": None, "hotkey": f"5Hot{b}", "team": False, "image": ref, "run_image": ref,
                       "digest": "sha256:" + ref.rsplit(":", 1)[1] if ref else None, "commit_block": 100 + b,
                       "score": sum(succ) / count, "successes": sum(succ),
                       "mean_time_s": statistics.fmean(ok) if ok else None, "winner": False, "crowned": False,
                       "results": {"success": succ, "t": ts, "outcome": [r["outcome"] for r in got]}})
    ch = sorted((x for x in images if x["role"] == "challenger"), key=verify._rank_key)
    for r, x in enumerate(ch):
        x["rank"] = r + 1
    king = images[0] if images else {"hotkey": None, "score": None}
    beats = bool(ch) and ch[0]["score"] > 0 and ch[0]["score"] > (king["score"] or 0) + 0.02
    if ch:
        ch[0]["winner"], ch[0]["crowned"] = True, beats
    return {"format": "vector-round-verify/1", "skill": "bin-picking", "round": n, "status": "final", "note": None,
            "salt": SALT, "seed_rule": "...", "seed_tries": 6, "draw": draw,
            "task": {"time_limit_s": 15.0, "human_factor": 2.0}, "wall_s": 45.0, "env": dict(ENV),
            "scene_count": count, "scenes_sha256": verify.scenes_digest(rows), "scenes": rows, "margin": 0.02,
            "decision": {"king": king["hotkey"], "king_score": king["score"], "winner": ch[0]["hotkey"] if ch else None,
                         "winner_score": ch[0]["score"] if ch else None, "beats_king": beats, "crowned": beats},
            "images": images}


def serve(monkeypatch, doc, final=True):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path == f"/v1/skills/bin-picking/expert/rounds/{doc['round']}/verify" and final:
            return httpx.Response(200, json=doc)
        return httpx.Response(404, json={"detail": "the round is not final yet"})
    monkeypatch.setattr(api_mod, "make_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(handler)))
    return seen


@pytest.fixture
def env(monkeypatch):
    e = fake_env(broken={verify.base_seed(SALT, 4, 2), verify.base_seed(SALT, 4, 2) + 1})
    monkeypatch.setattr(verify, "load_env", lambda: e)
    return e


def run(capsys, *argv):
    code = cli.main(["verify", *argv])
    cap = capsys.readouterr()
    return code, (json.loads(cap.out) if cap.out.strip() else None), cap.err


def test_verify_regenerates_the_round_and_reruns_an_image(monkeypatch, capsys, env):
    doc = make_doc(fake_env(broken=env.scenes.broken))
    assert doc["scenes"][2]["seed"] == verify.base_seed(SALT, 4, 2) + 2
    seen = serve(monkeypatch, doc)
    code, out, err = run(capsys, "--round", "4", "--api", "http://api", "--image", "sha256:" + "a" * 64,
                         "--command", "fake rate 0.8", "--scenes", "5")
    assert code == 0, err
    assert seen == ["/v1/skills/bin-picking/expert/rounds/4/verify"]
    assert out["ok"] and out["document"] == {"ok": True, "problems": []} and out["env"]["match"]
    assert out["salt"] == SALT and out["scenes"] == {"checked": 12, "matched": 12, "mismatched": []}
    rr = out["rerun"]
    assert rr["image"] == IMG and rr["ran"] == "command" and rr["scenes"] == 5
    assert rr["agreement"] == rr["outcome_agreement"] == 1.0 and rr["disagreements"] == [] and rr["mean_abs_dt_s"] in (0.0, None)
    assert "scene 12/12" in err


def test_a_partial_check_and_a_disagreeing_rerun(monkeypatch, capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=env.scenes.broken))
    serve(monkeypatch, doc)
    saved = tmp_path / "round4.json"
    code, out, _ = run(capsys, "--round", "4", "--api", "http://api", "--save", str(saved), "--hash-scenes", "3",
                       "--image", IMG, "--command", "fake rate 0.05", "--scenes", "8", "--sample-seed", "7")
    assert code == 0 and json.loads(saved.read_text()) == doc
    assert out["scenes"]["checked"] == 8 and out["scenes"]["matched"] == 8
    rr = out["rerun"]
    assert rr["agreement"] < 1.0 and rr["disagreements"]
    assert rr["published_successes"] - rr["local_successes"] == len(rr["disagreements"])
    for d in rr["disagreements"]:
        assert d["published"]["success"] and not d["local"]["success"]
    env.scenes.calls.clear()
    code2, out2, _ = run(capsys, "--round", "4", "--doc", str(saved), "--hash-scenes", "3", "--image", IMG,
                         "--command", "fake rate 0.05", "--scenes", "8", "--sample-seed", "7")
    assert code2 == 0 and out2["rerun"] == rr


def test_not_final_unknown_image_and_bad_arguments(monkeypatch, capsys, env):
    doc = make_doc(fake_env(broken=env.scenes.broken))
    serve(monkeypatch, doc, final=False)
    code, out, err = run(capsys, "--round", "4", "--api", "http://api")
    assert code == 2 and out is None and "round 4: the round is not final yet" in err
    serve(monkeypatch, doc)
    code, _, err = run(capsys, "--round", "4", "--api", "http://api", "--image", "ghcr.io/eve/x@sha256:" + "f" * 64)
    assert code == 2 and "was not evaluated in round 4" in err and IMG in err
    code, _, err = run(capsys, "--round", "4", "--api", "http://api", "--image", "5Hot0")
    assert code == 2 and "not published" in err
    with pytest.raises(SystemExit, match="--image"):
        cli.main(["verify", "--round", "4", "--api", "http://api", "--command", "x"])
    monkeypatch.delenv("VECTOR_MINER_API", raising=False)
    with pytest.raises(SystemExit, match="--api"):
        cli.main(["verify", "--round", "4"])


def _tampered(doc, fn):
    d = copy.deepcopy(doc)
    fn(d)
    return d


@pytest.mark.parametrize("name,fn,problem", [
    ("score", lambda d: d["images"][1].__setitem__("score", 0.99), "score 0.99 is not"),
    ("successes", lambda d: d["images"][1]["results"]["success"].__setitem__(0, 1 - d["images"][1]["results"]["success"][0]),
     "successes published"),
    ("seed", lambda d: (d["scenes"][5].__setitem__("seed", 12345), d.__setitem__("scenes_sha256", verify.scenes_digest(d["scenes"]))),
     "does not derive from the salt"),
    ("list", lambda d: d["scenes"][1].__setitem__("state_hash", "0" * 16), "scenes_sha256 does not match"),
    ("crown", lambda d: (d["decision"].__setitem__("king_score", 0.99), d["images"][0].__setitem__("score", 0.99)),
     "crowned without beating"),
    ("rank", lambda d: [x.__setitem__("rank", 3 - x["rank"]) for x in d["images"] if x["rank"]], "ranks do not follow"),
    ("status", lambda d: d.__setitem__("status", "evaluating"), "not final"),
])
def test_the_document_is_checked(capsys, env, tmp_path, name, fn, problem):
    doc = _tampered(make_doc(fake_env(broken=env.scenes.broken)), fn)
    p = tmp_path / "d.json"
    p.write_text(json.dumps(doc))
    code, out, _ = run(capsys, "--round", "4", "--doc", str(p), "--hash-scenes", "0")
    assert code == 1 and not out["ok"] and any(problem in x for x in out["document"]["problems"]), out["document"]


def test_a_changed_scene_hash_is_found(capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=env.scenes.broken))
    doc["scenes"][7]["state_hash"] = "f" * 16
    doc["scenes_sha256"] = verify.scenes_digest(doc["scenes"])
    p = tmp_path / "d.json"
    p.write_text(json.dumps(doc))
    code, out, _ = run(capsys, "--round", "4", "--doc", str(p))
    assert code == 1 and out["document"]["ok"]
    assert out["scenes"]["matched"] == 11 and out["scenes"]["mismatched"][0]["index"] == 7
    assert out["scenes"]["mismatched"][0]["got_state_hash"] != "f" * 16
    e = fake_env(env={**ENV, "assets_sha256": "1" * 64})
    assert not verify.verify(make_doc(e), e=e, hash_scenes=1, log=lambda *x: None)["env"]["match"]


def test_a_real_round_regenerates_with_vector_env(capsys, tmp_path):
    pytest.importorskip("vector_env")
    from vector_env import assets, harness, scenes
    from vector_env.settings import env_config
    try:
        assets.ensure()
    except Exception as e:
        pytest.skip(f"no asset bundle: {e}")
    real = SimpleNamespace(scenes=scenes, harness=harness, wall_s=20 * float(env_config()["harness"]["wall_s"]))
    cmd = ["node", str(HERE.parent / "example" / "solve.mjs")]
    doc = make_doc(real, n=2, count=2, specs=())
    made = [scenes.generate(x["seed"]) for x in doc["scenes"]]
    got = harness.run({"command": cmd}, made, time_limit_s=15.0, wall_s=real.wall_s)
    succ = [int(r["success"]) for r in got]
    ok = [r["t"] for r in got if r["success"]]
    doc["env"] = scenes.env_info()
    doc["images"] = [{"role": "challenger", "rank": 1, "hotkey": "5Hot", "team": False, "image": IMG, "run_image": IMG,
                      "digest": "sha256:" + "a" * 64, "commit_block": 1, "score": sum(succ) / 2, "successes": sum(succ),
                      "mean_time_s": statistics.fmean(ok) if ok else None, "winner": True, "crowned": False,
                      "results": {"success": succ, "t": [r["t"] for r in got], "outcome": [r["outcome"] for r in got]}}]
    doc["decision"] = {"king": None, "king_score": None, "winner": "5Hot", "winner_score": sum(succ) / 2,
                       "beats_king": sum(succ) > 0, "crowned": False}
    p = tmp_path / "d.json"
    p.write_text(json.dumps(doc))
    code, out, err = run(capsys, "--round", "2", "--doc", str(p), "--image", IMG, "--command", " ".join(cmd),
                         "--scenes", "2", "--wall", str(real.wall_s))
    assert code == 0, (out, err)
    assert out["env"]["match"] and out["scenes"]["matched"] == 2 and out["rerun"]["scenes"] == 2
    assert out["rerun"]["agreement"] == 1.0, out["rerun"]
