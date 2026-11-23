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
    rows, made, skipped = [], [], []
    for i in range(count):
        sc = None
        for k in range(6):
            try:
                sc = e.scenes.generate(verify.base_seed(SALT, n, i) + k, draw)
                break
            except Exception:
                continue
        if sc is None:
            skipped.append(i)
            continue
        made.append(sc)
        rows.append({"index": i, "seed": sc["seed"], "state_hash": sc["state_hash"], "parts": 3})
    count = len(rows)
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
            "closed_at": "2026-11-20T00:00:00+00:00", "salt": SALT, "salt_sha256": hashlib.sha256(SALT.encode()).hexdigest(),
            "salt_committed_at": "2026-11-19T00:00:00+00:00", "seed_rule": "...", "seed_tries": 6, "skipped": skipped, "draw": draw,
            "task": {"time_limit_s": 15.0, "human_factor": 2.0}, "wall_s": 45.0, "env": dict(ENV),
            "scene_count": count, "scenes_sha256": verify.scenes_digest(rows), "scenes": rows, "margin": 0.02,
            "decision": {"king": king["hotkey"], "king_score": king["score"], "winner": ch[0]["hotkey"] if ch else None,
                         "winner_score": ch[0]["score"] if ch else None, "beats_king": beats, "crowned": beats},
            "images": images}


def serve(monkeypatch, doc, final=True, view=None):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path == f"/v1/skills/bin-picking/expert/rounds/{doc['round']}/verify" and final:
            return httpx.Response(200, json=doc)
        if req.url.path == f"/v1/skills/bin-picking/expert/rounds/{doc['round']}" and view is not None:
            return httpx.Response(200, json=view)
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
    assert seen == ["/v1/skills/bin-picking/expert/rounds/4/verify", "/v1/skills/bin-picking/expert/rounds/4"]
    assert out["ok"] and out["document"] == {"ok": True, "problems": []} and out["env"]["match"]
    assert out["salt"] == SALT and out["scenes"] == {"checked": 12, "matched": 12, "mismatched": [], "unverified": [],
                                                         "skipped_checked": 0}
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


def test_not_final_unknown_image_and_bad_arguments(monkeypatch, capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=env.scenes.broken))
    serve(monkeypatch, doc, final=False)
    code, out, err = run(capsys, "--round", "4", "--api", "http://api")
    assert code == 2 and out is None and "round 4: the round is not final yet" in err
    serve(monkeypatch, doc)
    code, _, err = run(capsys, "--round", "4", "--api", "http://api", "--image", "ghcr.io/eve/x@sha256:" + "f" * 64)
    assert code == 2 and "was not evaluated in round 4" in err and IMG in err
    code, _, err = run(capsys, "--round", "4", "--api", "http://api", "--image", "5Hot0")
    assert code == 2 and "not published" in err
    code, out, err = run(capsys, "--round", "4", "--api", "http://api", "--command", "x")
    assert code == 2 and out is None and "--image" in err
    monkeypatch.delenv("VECTOR_MINER_API", raising=False)
    code, out, err = run(capsys, "--round", "4")
    assert code == 2 and out is None and "--api" in err
    other = tmp_path / "round5.json"
    other.write_text(json.dumps({**doc, "round": 5}))
    code, out, err = run(capsys, "--round", "4", "--doc", str(other))
    assert code == 2 and out is None and "is round 5, not 4" in err
    code, out, err = run(capsys, "--round", "4", "--doc", str(tmp_path / "missing.json"))
    assert code == 2 and out is None and "cannot read" in err
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    code, out, err = run(capsys, "--round", "4", "--doc", str(broken))
    assert code == 2 and out is None and "cannot read" in err
    with pytest.raises(SystemExit) as e:
        cli.main(["verify"])
    assert e.value.code == 2


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


def rescore(doc):
    count = doc["scene_count"]
    for im in doc["images"]:
        res = im["results"]
        ok = [t for v, t in zip(res["success"], res["t"]) if v]
        im["successes"], im["score"] = sum(res["success"]), sum(res["success"]) / count
        im["mean_time_s"] = statistics.fmean(ok) if ok else None
        im["winner"] = im["crowned"] = False
    ch = sorted((x for x in doc["images"] if x["role"] == "challenger"), key=verify._rank_key)
    for r, x in enumerate(ch):
        x["rank"] = r + 1
    best = next((x for x in ch if x.get("status") != "infra_failed"), None)
    king = next((x for x in doc["images"] if x["role"] == "king"), None)
    beats = best is not None and best["score"] > 0 and best["score"] > (king["score"] if king else 0) + doc["margin"]
    if best:
        best["winner"], best["crowned"] = True, beats
    doc["decision"].update(winner=best["hotkey"] if best else None, winner_score=best["score"] if best else None,
                           king_score=king["score"] if king else None, beats_king=beats, crowned=beats)
    return doc


def write(tmp_path, doc, name="d.json"):
    p = tmp_path / name
    p.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return str(p)


@pytest.mark.parametrize("name,fn,where", [
    ("no salt", lambda d: d.pop("salt"), "salt"),
    ("no results", lambda d: d["images"][1].pop("results"), "images[1].results"),
    ("string seed", lambda d: d["scenes"][3].__setitem__("seed", "12"), "scenes[3].seed"),
    ("float count", lambda d: d.__setitem__("scene_count", 12.5), "scene_count"),
    ("scenes object", lambda d: d.__setitem__("scenes", {"0": 1}), "scenes"),
    ("success 7", lambda d: d["images"][0]["results"]["success"].__setitem__(0, 7), "success[0]"),
    ("image string", lambda d: d["images"].__setitem__(0, "x"), "images[0]"),
    ("bool round", lambda d: d.__setitem__("round", True), "round"),
    ("bad time", lambda d: d.__setitem__("salt_committed_at", "yesterday"), "salt_committed_at"),
    ("huge tries", lambda d: d.__setitem__("seed_tries", 10**9), "seed_tries"),
])
def test_a_malformed_document_cannot_be_checked(capsys, env, tmp_path, name, fn, where):
    doc = _tampered(make_doc(fake_env(broken=env.scenes.broken)), fn)
    if name == "bool round":
        doc["round"] = 4
        p = write(tmp_path, json.dumps(doc).replace('"round": 4', '"round": true', 1))
    else:
        p = write(tmp_path, doc)
    code, out, err = run(capsys, "--round", "4", "--doc", p)
    assert code == 2 and out is None and "malformed verify document" in err and where in err, err
    assert "Traceback" not in err


def test_hostile_json_is_refused(capsys, env, tmp_path, monkeypatch):
    doc = make_doc(fake_env(broken=env.scenes.broken))
    nan = json.dumps(doc).replace('"margin": 0.02', '"margin": NaN')
    for text, msg in ((nan, "NaN"), ("[" * 100000 + "]" * 100000, "not JSON"), ("[1, 2]", "not a verify document")):
        code, out, err = run(capsys, "--round", "4", "--doc", write(tmp_path, text))
        assert code == 2 and out is None and msg in err, err
    monkeypatch.setattr(verify, "MAX_DOC_BYTES", 1000)
    code, out, err = run(capsys, "--round", "4", "--doc", write(tmp_path, doc))
    assert code == 2 and "larger than 1000 bytes" in err


def test_a_missing_index_is_a_document_problem_not_a_crash(capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=env.scenes.broken))
    doc["scenes"][11]["index"] = 40
    doc["scenes_sha256"] = verify.scenes_digest(doc["scenes"])
    code, out, err = run(capsys, "--round", "4", "--doc", write(tmp_path, doc))
    assert code == 1 and any("indexes 0..11" in x for x in out["document"]["problems"]), err


def test_api_failures_exit_2(monkeypatch, capsys, env):
    def handler(kind):
        def h(req):
            if kind == "text":
                return httpx.Response(200, content=b"<html>")
            if kind == "big":
                return httpx.Response(200, content=b"x" * 5000)
            raise httpx.ConnectError("refused", request=req)
        return h
    for kind, msg in (("text", "not JSON"), ("big", "larger than 4000 bytes"), ("down", "cannot reach")):
        monkeypatch.setattr(verify, "MAX_DOC_BYTES", 4000)
        monkeypatch.setattr(api_mod, "make_client", lambda timeout, k=kind: httpx.Client(transport=httpx.MockTransport(handler(k))))
        code, out, err = run(capsys, "--round", "4", "--api", "http://api")
        assert code == 2 and out is None and msg in err, (kind, err)


def test_bad_arguments_exit_2(capsys, env, tmp_path):
    p = write(tmp_path, make_doc(fake_env(broken=env.scenes.broken)))
    for argv, msg in ((["--workers", "0"], "--workers"), (["--wall", "-1"], "--wall"), (["--scenes", "-3"], "negative"),
                      (["--salt-sha256", "abc"], "--salt-sha256"), (["--save", str(tmp_path / "no" / "x.json")], "cannot write")):
        code, out, err = run(capsys, "--round", "4", "--doc", p, *argv)
        assert code == 2 and out is None and msg in err, (argv, err)


class BrokenScenes(Scenes):
    def generate(self, seed, draw=None):
        raise RuntimeError("sim.mjs stopped: cannot open the bundle")


def test_a_broken_local_setup_is_not_a_mismatch(monkeypatch, capsys, tmp_path):
    doc = make_doc(fake_env())
    p = write(tmp_path, doc)
    monkeypatch.setattr(verify, "load_env", lambda: SimpleNamespace(scenes=BrokenScenes(), harness=Harness(), wall_s=45.0))
    code, out, err = run(capsys, "--round", "4", "--doc", p)
    assert code == 2 and out is None and "the scene generator does not work here" in err and "MISMATCH" not in err

    class NoBundle(Scenes):
        def env_info(self):
            raise RuntimeError("no asset bundle at /nonexistent")
    monkeypatch.setattr(verify, "load_env", lambda: SimpleNamespace(scenes=NoBundle(), harness=Harness(), wall_s=45.0))
    code, out, err = run(capsys, "--round", "4", "--doc", p)
    assert code == 2 and "asset bundle" in err

    def boom():
        raise verify.VerifyError("vector-env's settings do not load: bad yaml")
    monkeypatch.setattr(verify, "load_env", boom)
    code, out, err = run(capsys, "--round", "4", "--doc", p)
    assert code == 2 and "settings do not load" in err


def test_one_unreproducible_scene_with_a_working_generator_is_a_mismatch(monkeypatch, capsys, tmp_path):
    doc = make_doc(fake_env())
    bad = {verify.base_seed(SALT, 4, 6) + k for k in range(6)}
    monkeypatch.setattr(verify, "load_env", lambda: fake_env(broken=bad))
    code, out, _ = run(capsys, "--round", "4", "--doc", write(tmp_path, doc))
    assert code == 1 and [x["index"] for x in out["scenes"]["mismatched"]] == [6]
    assert "could not place" in out["scenes"]["mismatched"][0]["error"]


@pytest.mark.parametrize("ref,why", [
    ("ghcr.io/alice/bin-expert:latest", "not an accepted pinned image"),
    ("docker.io/alice/bin-expert@sha256:" + "a" * 64, "not accepted"),
    (IMG_B, "not pinned to the image's digest"),
])
def test_run_image_must_be_pinned_to_its_digest(monkeypatch, capsys, env, tmp_path, ref, why):
    doc = make_doc(fake_env(broken=env.scenes.broken))
    doc["images"][1]["run_image"] = ref
    pulled = []
    env.harness.pull = lambda image, docker: pulled.append(image)
    code, out, err = run(capsys, "--round", "4", "--doc", write(tmp_path, doc), "--image", "5Hot1", "--pull",
                         "--hash-scenes", "2")
    assert code == 1 and any(why in x for x in out["document"]["problems"]), out["document"]
    assert out["rerun"]["skipped"] and pulled == []


def test_pull_and_harness_failures_exit_2(capsys, env, tmp_path):
    p = write(tmp_path, make_doc(fake_env(broken=env.scenes.broken)))

    def pull(image, docker):
        raise RuntimeError("docker pull failed: denied")
    env.harness.pull = pull
    code, out, err = run(capsys, "--round", "4", "--doc", p, "--image", IMG, "--pull", "--hash-scenes", "2")
    assert code == 2 and "cannot pull" in err and "denied" in err
    del env.harness.pull

    def no_docker(*a, **k):
        raise RuntimeError("the docker daemon is not reachable")
    env.harness.run = no_docker
    code, out, err = run(capsys, "--round", "4", "--doc", p, "--image", IMG, "--hash-scenes", "2")
    assert code == 2 and "did not start" in err

    def infra(expert, scenes, **k):
        return [{"scene_id": x["scene_id"], "success": False, "outcome": "docker_unavailable", "infra": True,
                 "error": "no docker"} for x in scenes]
    env.harness.run = infra
    code, out, err = run(capsys, "--round", "4", "--doc", p, "--image", IMG, "--hash-scenes", "2")
    assert code == 2 and "docker_unavailable" in err


def test_the_salt_commitment_is_checked(monkeypatch, capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=env.scenes.broken))
    good = doc["salt_sha256"]
    for fn, msg in ((lambda d: d.pop("salt_sha256"), "no salt_sha256"),
                    (lambda d: d.__setitem__("salt_sha256", "0" * 64), "is not the sha256 of the salt"),
                    (lambda d: d.__setitem__("salt_committed_at", "2026-11-21T00:00:00+00:00"), "after the round closed")):
        code, out, _ = run(capsys, "--round", "4", "--doc", write(tmp_path, _tampered(doc, fn)), "--hash-scenes", "0")
        assert code == 1 and any(msg in x for x in out["document"]["problems"]), out["document"]
    p = write(tmp_path, doc)
    code, out, _ = run(capsys, "--round", "4", "--doc", p, "--hash-scenes", "0", "--salt-sha256", good.upper())
    assert code == 0, out
    code, out, _ = run(capsys, "--round", "4", "--doc", p, "--hash-scenes", "0", "--salt-sha256", "1" * 64)
    assert code == 1 and any("the commitment you gave" in x for x in out["document"]["problems"])
    serve(monkeypatch, doc, view={"round": {"n": 4, "salt_sha256": "2" * 64}})
    code, out, _ = run(capsys, "--round", "4", "--api", "http://api", "--hash-scenes", "0")
    assert code == 1 and any("of the round view" in x for x in out["document"]["problems"])
    serve(monkeypatch, doc, view={"round": {"n": 4, "salt_sha256": good}})
    code, out, _ = run(capsys, "--round", "4", "--api", "http://api", "--hash-scenes", "0")
    assert code == 0, out


def test_the_decision_needs_its_king_image(capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=env.scenes.broken))
    doc["images"] = doc["images"][1:]
    code, out, _ = run(capsys, "--round", "4", "--doc", write(tmp_path, doc), "--hash-scenes", "0")
    assert code == 1 and any("no king image was evaluated" in x for x in out["document"]["problems"])


def test_a_scene_made_with_another_vector_env_is_unverified_not_a_mismatch(monkeypatch, capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=env.scenes.broken))
    doc["scenes"][4]["env"] = {**ENV, "version": "0.0.9"}
    doc["scenes"][4]["state_hash"] = "9" * 16
    doc["scenes_sha256"] = verify.scenes_digest(doc["scenes"])
    code, out, err = run(capsys, "--round", "4", "--doc", write(tmp_path, doc))
    assert code == 2 and not out["ok"] and out["document"]["ok"] and out["scenes"]["mismatched"] == []
    assert [x["index"] for x in out["scenes"]["unverified"]] == [4] and "another vector-env" in err


def test_skipped_scenes(monkeypatch, capsys, env, tmp_path):
    skip = env.scenes.broken | {verify.base_seed(SALT, 4, 5) + k for k in range(6)}
    doc = make_doc(fake_env(broken=skip))
    assert doc["skipped"] == [5] and doc["scene_count"] == 11
    p = write(tmp_path, doc)
    monkeypatch.setattr(verify, "load_env", lambda: fake_env(broken=skip))
    code, out, err = run(capsys, "--round", "4", "--doc", p, "--image", IMG, "--command", "fake rate 0.8")
    assert code == 0 and out["scenes"]["checked"] == 11 and out["scenes"]["skipped_checked"] == 1, (out, err)
    code, out, _ = run(capsys, "--round", "4", "--doc", p, "--hash-scenes", "3")
    assert code == 0 and out["scenes"]["skipped_checked"] == 0
    monkeypatch.setattr(verify, "load_env", lambda: env)
    code, out, _ = run(capsys, "--round", "4", "--doc", p)
    assert code == 1 and out["scenes"]["mismatched"] == [{"index": 5, "skipped": True, "got_seed": verify.base_seed(SALT, 4, 5),
                                                          "got_state_hash": out["scenes"]["mismatched"][0]["got_state_hash"]}]
    d = _tampered(doc, lambda d: d.__setitem__("skipped", [5, 6]))
    code, out, _ = run(capsys, "--round", "4", "--doc", write(tmp_path, d), "--hash-scenes", "0")
    assert code == 1 and any("less the skipped ones" in x for x in out["document"]["problems"])


def test_stopped_and_infra_failed_images_are_reported(capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=env.scenes.broken))
    b = next(x for x in doc["images"] if x["image"] == IMG_B)
    for k in range(6, 12):
        b["results"]["success"][k], b["results"]["t"][k], b["results"]["outcome"][k] = 0, None, "not_run"
    b["status"], b["stop_reason"] = "stopped", "used its wall budget of 360 s after 6 of 12 scenes"
    a = next(x for x in doc["images"] if x["image"] == IMG)
    a["status"], a["infra_failures"] = "infra_failed", 5
    rescore(doc)
    assert b["winner"] and not a["winner"]
    code, out, err = run(capsys, "--round", "4", "--doc", write(tmp_path, doc), "--image", IMG_B,
                         "--command", "fake rate 0.5", "--scenes", "12")
    assert code == 0, (out, err)
    st = {x["image"]: x for x in out["unfinished"]}
    assert st[IMG_B]["status"] == "stopped" and st[IMG_B]["not_run"] == 6 and "wall budget" in st[IMG_B]["stop_reason"]
    assert st[IMG]["status"] == "infra_failed" and st[IMG]["infra_failures"] == 5
    assert out["rerun"]["scenes"] == 6 and "is stopped: 6 scenes not run" in err
    a["winner"], b["winner"] = True, False
    code, out, _ = run(capsys, "--round", "4", "--doc", write(tmp_path, doc), "--hash-scenes", "0")
    assert code == 1 and any("not marked as the winner" in x for x in out["document"]["problems"])
    doc["status"] = "review"
    code, out, _ = run(capsys, "--round", "4", "--doc", write(tmp_path, doc), "--hash-scenes", "0")
    assert code == 1 and any("the round is review, not final" in x for x in out["document"]["problems"])


def test_an_unexpected_error_still_exits_2(monkeypatch, capsys, env, tmp_path):
    def boom(*a, **k):
        raise ZeroDivisionError("surprise")
    monkeypatch.setattr(verify, "verify", boom)
    code, out, err = run(capsys, "--round", "4", "--doc", write(tmp_path, make_doc(fake_env(broken=env.scenes.broken))))
    assert code == 2 and "ZeroDivisionError" in err


def test_the_genesis_team_expert_is_checked_but_not_rerunnable(capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=env.scenes.broken))
    king = doc["images"][0]
    king.update(team=True, image=None, run_image=None, digest=None)
    p = write(tmp_path, doc)
    code, out, err = run(capsys, "--round", "4", "--doc", p)
    assert code == 0 and out["ok"] and out["document"]["problems"] == [], (out, err)
    assert out["scenes"]["matched"] == 12
    assert out["not_rerunnable"] == [{"role": "king", "hotkey": "5Hot0", "note": "team expert (not publicly re-runnable)"}]
    code, out, err = run(capsys, "--round", "4", "--doc", p, "--image", "5Hot0")
    assert code == 2 and "team expert (not publicly re-runnable)" in err
    code, out, err = run(capsys, "--round", "4", "--doc", p, "--image", "5Hot0", "--command", "fake rate 0.3",
                         "--hash-scenes", "2")
    assert code == 0 and out["rerun"]["image"] == "king 5Hot0: team expert (not publicly re-runnable)"
    king["score"] = 0.99
    code, out, _ = run(capsys, "--round", "4", "--doc", write(tmp_path, doc), "--hash-scenes", "0")
    assert code == 1 and any("king_score" in x for x in out["document"]["problems"])
