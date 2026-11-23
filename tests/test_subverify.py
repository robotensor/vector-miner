from __future__ import annotations

import copy
import hashlib
import json
import random
import statistics
from types import SimpleNamespace

import httpx
import pytest
from test_verify import ENV, SALT, Scenes, _h

from vector_miner import api as api_mod
from vector_miner import cli, subverify, verify

SUB = "ghcr.io/alice/bin-expert@sha256:" + "a" * 64
KING = "ghcr.io/kate/bin-expert@sha256:" + "c" * 64
SID = "sub-42"


class Harness:
    rates = {SUB: 0.8, KING: 0.4}

    def run(self, expert, scenes, *, time_limit_s, wall_s, workers=1, docker=None, on_result=None):
        out = []
        cmd = expert.get("command") or []
        ref = expert.get("image") or (cmd[cmd.index("as") + 1] if "as" in cmd else "x")
        for sc in scenes:
            ok = _h("scene", ref, sc["seed"]) < self.rates.get(ref, 0.0)
            tag = f"{ref}:{sc['seed']}" + (f":{random.random()}" if "noisy" in cmd else "")
            r = {"scene_id": sc["scene_id"], "success": ok, "outcome": "success" if ok else "missed",
                 "t": round(4 + 8 * _h("t", ref, sc["seed"]), 3) if ok else 0.0, "infra": False,
                 "action_hash": hashlib.sha256(tag.encode()).hexdigest()[:16]}
            out.append(r)
            if on_result:
                on_result(len(out) - 1, r)
        return out


def fake_env(**kw) -> SimpleNamespace:
    return SimpleNamespace(scenes=Scenes(**kw), harness=Harness(), wall_s=45.0)


def _part(e, ref, made, count):
    got = e.harness.run({"image": ref}, made, time_limit_s=15, wall_s=45)
    succ = [int(r["success"]) for r in got]
    ok = [r["t"] for r in got if r["success"]]
    return {"score": sum(succ) / count, "successes": sum(succ), "mean_time_s": statistics.fmean(ok) if ok else None,
            "results": {"success": succ, "t": [r["t"] for r in got], "outcome": [r["outcome"] for r in got],
                        "action_hash": [r["action_hash"] for r in got]}}


def make_doc(e, status="crowned", count=12, king_best=0.4):
    rows, made, skipped = [], [], []
    for i in range(count):
        sc = None
        for k in range(6):
            try:
                sc = e.scenes.generate(verify.base_seed(SALT, 0, i) + k, None)
                break
            except Exception:
                continue
        if sc is None:
            skipped.append(i)
            continue
        made.append(sc)
        rows.append({"index": i, "seed": sc["seed"], "state_hash": sc["state_hash"]})
    count = len(rows)
    p1 = _part(e, SUB, made, count)
    if status == "below_king":
        king_best = p1["score"]
    sample = [x["index"] for x in rows][::2]
    pos = {x["index"]: k for k, x in enumerate(rows)}
    p2 = None if status == "below_king" else {
        "status": "failed" if status == "disqualified" else "passed",
        "reason": "the action hash differs on 1 of 6 scenes" if status == "disqualified" else None,
        "scenes": sample, "action_hash": [p1["results"]["action_hash"][pos[i]] for i in sample],
        "wall_s": [12.5 for _ in sample]}
    if status == "disqualified":
        p2["action_hash"][1] = "0" * 16
    king = None
    if status in ("crowned", "not_crowned"):
        king = {"hotkey": "5Kate", "image": KING, "digest": "sha256:" + "c" * 64, **_part(e, KING, made, count)}
        if status == "not_crowned":
            king = {**king, **{k: p1[k] for k in ("score", "successes", "mean_time_s", "results")}}
    beats = king is not None and p1["score"] > king["score"] + 0.02
    return {"format": subverify.FORMAT, "skill": "bin-picking",
            "submission": {"id": SID, "hotkey": "5Alice", "image": SUB, "digest": "sha256:" + "a" * 64,
                           "status": status, "reason": p2["reason"] if status == "disqualified" else None,
                           "committed_at": "2026-11-23T01:00:00+00:00", "finished_at": "2026-11-23T09:00:00+00:00"},
            "salt": SALT, "salt_sha256": hashlib.sha256(SALT.encode()).hexdigest(),
            "salt_committed_at": "2026-11-23T01:00:01+00:00", "phase1_started_at": "2026-11-23T01:05:00+00:00",
            "seed_rule": "...", "seed_tries": 6, "skipped": skipped, "draw": None, "task": {"time_limit_s": 15.0},
            "wall_s": 45.0, "step_deadline_s": 2.0, "env": dict(ENV), "scene_count": count,
            "scenes_sha256": verify.scenes_digest(rows), "scenes": rows, "margin": 0.02, "phase1": p1, "phase2": p2,
            "king": king, "decision": {"king": "5Kate", "king_best": king_best, "phase2_ran": p2 is not None,
                                       "beats_king": beats, "crowned": status == "crowned"}}


BROKEN = {verify.base_seed(SALT, 0, 2) + k for k in range(6)}


@pytest.fixture
def env(monkeypatch):
    e = fake_env(broken=BROKEN)
    monkeypatch.setattr(verify, "load_env", lambda: e)
    return e


def write(tmp_path, doc, name="doc.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return str(p)


def run(capsys, *argv):
    code = cli.main(["verify", *argv])
    cap = capsys.readouterr()
    return code, (json.loads(cap.out) if cap.out.strip() else None), cap.err


def api(monkeypatch, doc, view=None, final=True):
    seen = []

    def handler(req):
        seen.append(req.url.path)
        if req.url.path == f"/v1/skills/bin-picking/expert/submissions/{SID}/verify" and final:
            return httpx.Response(200, json=doc)
        if req.url.path == f"/v1/skills/bin-picking/expert/submissions/{SID}" and view is not None:
            return httpx.Response(200, json=view)
        return httpx.Response(404, json={"detail": "the submission is not final yet"})
    monkeypatch.setattr(api_mod, "make_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(handler)))
    return seen


@pytest.mark.parametrize("status", ["crowned", "not_crowned", "below_king", "disqualified"])
def test_every_final_status_adds_up(capsys, env, tmp_path, status):
    doc = make_doc(fake_env(broken=BROKEN), status)
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc))
    assert code == 0 and out["ok"] and out["document"]["problems"] == [], out
    assert out["submission"] == SID and out["status"] == status and out["scenes"]["checked"] == doc["scene_count"]
    assert out["scenes"]["skipped_checked"] == 1 and out["scenes"]["mismatched"] == []
    if status == "below_king":
        assert out["phase2"] is None
    else:
        assert out["phase2"]["passed"] == (status != "disqualified")
        assert out["phase2"]["mismatched"] == ([doc["phase2"]["scenes"][1]] if status == "disqualified" else [])


def test_fetched_from_the_api_with_the_salt_of_the_submission_view(monkeypatch, capsys, env):
    doc = make_doc(fake_env(broken=BROKEN))
    seen = api(monkeypatch, doc, view={"id": SID, "salt_sha256": doc["salt_sha256"].upper()})
    code, out, err = run(capsys, "--submission", SID, "--api", "http://api", "--hash-scenes", "4")
    assert code == 0 and out["ok"] and out["scenes"]["checked"] == 4, out
    assert seen == [f"/v1/skills/bin-picking/expert/submissions/{SID}/verify",
                    f"/v1/skills/bin-picking/expert/submissions/{SID}"]
    api(monkeypatch, doc, view={"id": SID, "salt_sha256": "d" * 64})
    code, out, err = run(capsys, "--submission", SID, "--api", "http://api", "--hash-scenes", "0")
    assert code == 1 and any("of the submission view" in p for p in out["document"]["problems"]), out
    code, out, err = run(capsys, "--submission", SID, "--api", "http://api", "--hash-scenes", "0",
                         "--salt-sha256", "e" * 64)
    assert code == 1 and any("the commitment you gave" in p for p in out["document"]["problems"])
    api(monkeypatch, doc, final=False)
    code, out, err = run(capsys, "--submission", SID, "--api", "http://api")
    assert code == 2 and f"submission {SID}: the submission is not final yet" in err


def test_a_rerun_compares_action_hashes(capsys, env, tmp_path):
    p = write(tmp_path, make_doc(fake_env(broken=BROKEN)))
    code, out, err = run(capsys, "--submission", SID, "--doc", p, "--image", SUB, "--scenes", "5", "--hash-scenes", "0")
    r = out["rerun"]
    assert code == 0 and r["role"] == "submission" and r["scenes"] == 5 and r["hash_equal"] == 5, r
    assert r["hash_agreement"] == 1.0 and r["mismatched"] == []
    code, out, err = run(capsys, "--submission", SID, "--doc", p, "--image", "king", "--scenes", "4")
    assert code == 0 and out["rerun"]["role"] == "king" and out["rerun"]["hash_equal"] == 4, out["rerun"]
    code, out, err = run(capsys, "--submission", SID, "--doc", p, "--image", "submission", "--command",
                         f"x noisy as {SUB}", "--scenes", "3")
    r = out["rerun"]
    assert code == 0 and r["hash_equal"] == 0 and len(r["mismatched"]) == 3 and r["outcome_agreement"] == 1.0, r
    code, out, err = run(capsys, "--submission", SID, "--doc", p, "--image", "ghcr.io/eve/x@sha256:" + "f" * 64)
    assert code == 2 and f"did not run for submission {SID}" in err


def _t(doc, fn):
    d = copy.deepcopy(doc)
    fn(d)
    return d


def _set(path, v):
    def f(d):
        o = d
        for k in path[:-1]:
            o = o[k]
        o[path[-1]] = v
    return f


@pytest.mark.parametrize("status,fn,problem", [
    ("crowned", _set(["submission", "status"], "phase2"), "not final"),
    ("crowned", _set(["salt"], "ff" * 16), "not the sha256 of the salt"),
    ("crowned", _set(["salt_committed_at"], "2026-11-23T02:00:00+00:00"), "after phase 1 started"),
    ("crowned", _set(["salt_committed_at"], None), "--allow-uncommitted"),
    ("crowned", _set(["scenes_sha256"], "0" * 64), "scenes_sha256 does not match"),
    ("crowned", _set(["phase1", "score"], 0.99), "score 0.99 is not"),
    ("crowned", _set(["king", "score"], 0.95), "king run: score 0.95"),
    ("crowned", _set(["decision", "king_best"], 0.99), "is not above the king's best 0.99"),
    ("crowned", _set(["phase2", "action_hash", 0], "1" * 16), "phase 2 did not pass"),
    ("crowned", _set(["phase2", "wall_s", 0], 99.0), "phase 2 did not pass"),
    ("crowned", _set(["king"], None), "no king run on the same scenes"),
    ("crowned", _set(["decision", "crowned"], False), "decision.crowned disagrees"),
    ("crowned", _set(["submission", "image"], "ghcr.io/alice/bin-expert:latest"), "not an accepted pinned image"),
    ("not_crowned", _set(["submission", "status"], "crowned"), "says not_crowned"),
    ("below_king", _set(["decision", "king_best"], 0.1), "below_king, but the phase-1 score"),
    ("disqualified", lambda d: (d["phase2"].update(reason=None), d["submission"].update(reason=None)),
     "disqualified without a reason"),
    ("disqualified", _set(["phase2", "scenes", 0], 999), "not in the list"),
])
def test_the_document_is_checked(capsys, env, tmp_path, status, fn, problem):
    doc = _t(make_doc(fake_env(broken=BROKEN), status), fn)
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc), "--hash-scenes", "0")
    assert code == 1 and any(problem in p for p in out["document"]["problems"]), out["document"]


def test_disqualified_needs_a_real_phase2_difference(capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=BROKEN), "disqualified")
    pos = {x["index"]: k for k, x in enumerate(doc["scenes"])}
    doc["phase2"]["action_hash"] = [doc["phase1"]["results"]["action_hash"][pos[i]] for i in doc["phase2"]["scenes"]]
    code, out, _ = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc), "--hash-scenes", "0")
    assert code == 1 and any("disqualified, but every phase-2 action hash" in p for p in out["document"]["problems"])


def test_allow_uncommitted_and_malformed_documents(capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=BROKEN))
    doc["salt_committed_at"] = None
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc), "--allow-uncommitted",
                         "--hash-scenes", "0")
    assert code == 0 and out["document"]["warnings"] and "warning:" in err
    bad = make_doc(fake_env(broken=BROKEN))
    bad["phase1"]["results"]["action_hash"][0] = "XYZ"
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, bad))
    assert code == 2 and "phase1.results.action_hash[0]" in err
    other = make_doc(fake_env(broken=BROKEN))
    other["submission"]["id"] = "sub-1"
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, other))
    assert code == 2 and "is submission sub-1, not sub-42" in err
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, {"format": "vector-round-verify/1"}))
    assert code == 1 and "unknown document format" in out["document"]["problems"][0]


def test_a_changed_scene_hash_is_a_mismatch(capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=BROKEN))
    doc["scenes"][3]["state_hash"] = "0" * 16
    doc["scenes_sha256"] = verify.scenes_digest(doc["scenes"])
    code, out, _ = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc))
    assert code == 1 and [x["index"] for x in out["scenes"]["mismatched"]] == [doc["scenes"][3]["index"]]


def test_the_seed_rule_is_vector_envs():
    se = pytest.importorskip("vector_env.scenes")
    assert verify.base_seed(SALT, subverify.SEED_N, 7) == se.seed_for(0, 7, SALT)
