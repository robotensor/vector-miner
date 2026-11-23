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


def _part(e, ref, made, infra=()):
    got = e.harness.run({"image": ref}, made, time_limit_s=15, wall_s=45)
    for k in infra:
        got[k] = {"success": False, "t": None, "outcome": "harness_error", "action_hash": None}
    succ = [int(r["success"]) for r in got]
    ok = [r["t"] for r in got if r["success"]]
    counted = len(got) - len(infra)
    return {"score": sum(succ) / max(1, counted), "successes": sum(succ), "counted": counted, "infra": len(infra),
            "mean_time_s": statistics.fmean(ok) if ok else None,
            "results": {"success": succ, "t": [r["t"] for r in got], "outcome": [r["outcome"] for r in got],
                        "action_hash": [r["action_hash"] for r in got],
                        "rows_sent": [None if k in infra else 40 for k in range(len(got))]}}


def _sample(ok, k=6):
    return sorted(sorted(ok, key=lambda i: hashlib.sha256(f"{SALT}:phase2:{i}".encode()).hexdigest())[:k])


def make_doc(e, status="crowned", count=12, king_best=0.4, p1_infra=(), p2_infra=0, king_infra=(), genesis=False):
    late = status == "decisive_failed"
    status = "crowned" if late else status
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
        rows.append({"index": i, "seed": sc["seed"], "state_hash": sc["state_hash"], "parts": 6})
    count = len(rows)
    p1 = _part(e, SUB, made, p1_infra)
    if status == "below_king":
        king_best = p1["score"]
    res = p1["results"]
    pos = {x["index"]: k for k, x in enumerate(rows)}
    sample = _sample([x["index"] for k, x in enumerate(rows) if k not in p1_infra])
    p2 = None
    if status != "below_king":
        p2 = {"status": "failed" if status == "disqualified" else "passed",
              "reason": "1 of 6 phase-2 scenes gave other actions than in phase 1" if status == "disqualified" else None,
              "scenes": sample, "outcome": [res["outcome"][pos[i]] for i in sample],
              "action_hash": [res["action_hash"][pos[i]] for i in sample], "wall_s": [12.5 for _ in sample],
              "mismatched": [], "wall_timeouts": [], "sample_rule": "..."}
        for j in range(p2_infra):
            p2["outcome"][j], p2["action_hash"][j], p2["wall_s"][j] = "docker_unavailable", None, None
        if status == "disqualified" and p2_infra + 1 < len(sample):
            p2["action_hash"][p2_infra + 1] = "0" * 16
            p2["mismatched"] = [sample[p2_infra + 1]]
    king, left = None, set(p1_infra)
    if status in ("crowned", "not_crowned"):
        part = _part(e, KING, made, king_infra)
        if status == "not_crowned":
            part = {k: p1[k] for k in ("score", "successes", "mean_time_s", "results")}
        else:
            left |= set(king_infra)
        king = {"id": None if genesis else "sub-7", "hotkey": "5Kate", "team": genesis,
                "image": None if genesis else KING, "source_image": None if genesis else KING,
                "digest": None if genesis else "sha256:" + "c" * 64, "status": "king", "reason": None,
                **{k: part[k] for k in ("score", "successes", "mean_time_s", "results")}}
    pairs = [k for k in range(count) if k not in left]
    n = len(pairs)
    cs = sum(res["success"][k] for k in pairs) / n
    ks = sum(king["results"]["success"][k] for k in pairs) / n if king else None
    if king:
        king["score"] = ks
    beats = king is not None and cs > ks + 0.02
    decisive = None
    if beats:
        seen = set(p2["scenes"]) if p2 else set()
        d = sorted(rows[k]["index"] for k in pairs if res["success"][k] and not king["results"]["success"][k]
                   and rows[k]["index"] not in seen)
        decisive = {"status": "passed", "reason": None, "scenes": d,
                    "outcome": [res["outcome"][pos[i]] for i in d],
                    "action_hash": [res["action_hash"][pos[i]] for i in d], "wall_s": [12.5 for _ in d],
                    "mismatched": [], "wall_timeouts": [], "rule": "..."}
        if late:
            decisive["action_hash"][0] = "0" * 16
            decisive.update(status="failed", mismatched=[d[0]],
                            reason="1 of 2 decisive scenes (won against the king) gave other actions")
            status = "disqualified"
    doc_reason = (decisive or {}).get("reason") if late else (p2["reason"] if status == "disqualified" else None)
    return {"format": subverify.FORMAT, "skill": "bin-picking",
            "submission": {"id": SID, "hotkey": "5Alice", "uid": 4, "image": SUB, "digest": "sha256:" + "a" * 64,
                           "commit_block": 100, "status": status,
                           "reason": doc_reason, "mirror_url": None,
                           "committed_at": "2026-11-23T01:00:00+00:00", "scored_at": "2026-11-23T05:00:00+00:00",
                           "finished_at": "2026-11-23T09:00:00+00:00"},
            "salt": SALT, "salt_sha256": hashlib.sha256(SALT.encode()).hexdigest(),
            "salt_committed_at": "2026-11-23T01:00:01+00:00", "phase1_started_at": "2026-11-23T01:05:00+00:00",
            "seed_rule": "...", "seed_tries": 6, "skipped": skipped, "draw": None,
            "task": {"time_limit_s": 15.0, "human_factor": 2.0}, "wall_s": 45.0, "step_deadline_s": 2.0,
            "max_restarts": 2, "margin": 0.02, "env": dict(ENV), "scene_count": count,
            "scenes_sha256": verify.scenes_digest(rows), "scenes": rows, "phase1": p1, "phase2": p2, "king": king,
            "decisive": decisive,
            "decision": {"king_best": king_best, "king": "5Kate", "phase2_ran": p2 is not None,
                         "decisive_ran": decisive is not None,
                         "king_score": ks if king else None, "score": cs if king else None,
                         "compared": n if king else None, "beats_king": beats, "crowned": status == "crowned"}}


def test_crowned_needs_a_passed_decisive_recheck(capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=BROKEN), "crowned")
    assert doc["decisive"]["scenes"] and doc["decisive"]["status"] == "passed"
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc), "--hash-scenes", "0")
    assert code == 0 and out["decisive"]["passed"] and out["decisive"]["expected"] == doc["decisive"]["scenes"], out
    bad = _t(doc, _set(["decisive", "action_hash", 0], "1" * 16))
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, bad), "--hash-scenes", "0")
    assert code == 1 and any("decisive re-check did not pass" in p for p in out["document"]["problems"])
    short = _t(doc, lambda d: [d["decisive"][k].pop() for k in ("scenes", "outcome", "action_hash", "wall_s")])
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, short), "--hash-scenes", "0")
    assert code == 1 and any("not the recomputed decisive scenes" in p for p in out["document"]["problems"])
    gone = _t(doc, _set(["decisive"], None))
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, gone), "--hash-scenes", "0")
    assert code == 1 and any("no decisive re-check is published" in p for p in out["document"]["problems"])
    nc = make_doc(fake_env(broken=BROKEN), "not_crowned")
    assert nc["decisive"] is None
    nc["decisive"] = doc["decisive"]
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, nc), "--hash-scenes", "0")
    assert code == 1 and any("not_crowned, but a decisive re-check" in p for p in out["document"]["problems"])


def test_disqualified_at_the_decisive_recheck(capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=BROKEN), "decisive_failed")
    assert doc["submission"]["status"] == "disqualified" and doc["king"] is not None
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc), "--hash-scenes", "0")
    assert code == 0 and out["document"]["problems"] == [] and not out["decisive"]["passed"], out
    fake = _t(doc, lambda d: (d["decisive"].update(mismatched=[]), d["decisive"]["action_hash"].__setitem__(
        0, d["phase1"]["results"]["action_hash"][[x["index"] for x in d["scenes"]].index(d["decisive"]["scenes"][0])])))
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, fake), "--hash-scenes", "0")
    assert code == 1 and any("every decisive scene gave phase 1's action hash" in p for p in out["document"]["problems"])
    nod = _t(doc, _set(["decisive"], None))
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, nod), "--hash-scenes", "0")
    assert code == 1 and any("disqualified, but a king run is published" in p for p in out["document"]["problems"])


def test_disqualified_before_phase2_on_our_side(capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=BROKEN), "below_king")
    doc["submission"].update(status="disqualified", reason="image no longer available: manifest unknown")
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc), "--hash-scenes", "0")
    assert code == 0 and out["document"]["problems"] == [], out
    doc["submission"]["reason"] = "repeated failures on our side: 3 of 10 phase-1 scenes failed (after 2 reviews)"
    doc["phase1"]["score"] = None
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc), "--hash-scenes", "0")
    assert code == 0 and out["document"]["problems"] == [], out
    doc["submission"]["reason"] = "something else"
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc), "--hash-scenes", "0")
    assert code == 1 and any("no phase 2 is published" in p for p in out["document"]["problems"])


def test_margin_ties_are_exact():
    assert 14 / 100 > 12 / 100 + 0.02
    assert not subverify.beats(14, 12, 100, 0.02) and subverify.beats(15, 12, 100, 0.02)
    assert not subverify.above(0.14, 0.12, 0.02) and subverify.above(0.15, 0.12, 0.02)


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


def test_infrastructure_failures_are_left_out_as_the_server_does(capsys, env, tmp_path):
    plain = make_doc(fake_env(broken=BROKEN))
    pos = {x["index"]: k for k, x in enumerate(plain["scenes"])}
    full = _sample(list(pos))
    p1_infra = (pos[full[0]], pos[full[2]])
    doc = make_doc(fake_env(broken=BROKEN), "crowned", p1_infra=p1_infra, p2_infra=1, king_infra=(pos[full[4]],))
    count, p1, p2 = doc["scene_count"], doc["phase1"], doc["phase2"]
    assert p1["infra"] == 2 and p1["counted"] == count - 2 and p1["score"] == p1["successes"] / (count - 2)
    assert full[0] not in p2["scenes"] and p2["outcome"][0] == "docker_unavailable"
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc), "--hash-scenes", "0")
    assert code == 0 and out["ok"] and out["document"]["problems"] == [], out["document"]
    assert out["phase2"]["infra"] == [p2["scenes"][0]] and out["phase2"]["compared"] == len(p2["scenes"]) - 1
    assert out["phase2"]["passed"] and out["same_scenes"]["compared"] == count - 3 == doc["decision"]["compared"]
    assert out["same_scenes"]["score"] == doc["decision"]["score"]
    assert out["same_scenes"]["king_score"] == doc["king"]["score"] == doc["decision"]["king_score"]
    old = {"phase1": p1["successes"] / count,
           "king": sum(doc["king"]["results"]["success"]) / count,
           "sample": _sample(list(pos))}
    for fn, problem in ((_set(["phase1", "score"], old["phase1"]), "2 infrastructure failures left out"),
                        (_set(["king", "score"], old["king"]), "king run: score"),
                        (_set(["phase2", "scenes"], old["sample"]), "other scenes than the sample rule picks"),
                        (_set(["phase1", "infra"], 0), "0 infrastructure failures published, 2 in the results"),
                        (_set(["decision", "compared"], count), f"is not {count - 3}"),
                        (_set(["phase2", "mismatched"], [p2["scenes"][0]]), "infrastructure failures left out")):
        code, out, _ = run(capsys, "--submission", SID, "--doc", write(tmp_path, _t(doc, fn)), "--hash-scenes", "0")
        assert code == 1 and any(problem in p for p in out["document"]["problems"]), (problem, out["document"])
    code, out, _ = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc), "--image", SUB, "--scenes", "50",
                       "--hash-scenes", "0")
    assert code == 0 and out["rerun"]["scenes"] == count - 2 == out["rerun"]["hash_equal"], out["rerun"]


def test_a_phase2_that_only_hit_infrastructure_is_no_pass(capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=BROKEN), "disqualified", p2_infra=6)
    assert doc["phase2"]["mismatched"] == [] and set(doc["phase2"]["outcome"]) == {"docker_unavailable"}
    code, out, _ = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc), "--hash-scenes", "0")
    assert code == 0 and out["phase2"]["compared"] == 0 and not out["phase2"]["passed"], out


def test_the_genesis_king_has_no_image(capsys, env, tmp_path):
    p = write(tmp_path, make_doc(fake_env(broken=BROKEN), "crowned", genesis=True))
    code, out, err = run(capsys, "--submission", SID, "--doc", p, "--hash-scenes", "0")
    assert code == 0 and out["ok"] and out["document"]["problems"] == [], out["document"]
    assert out["document"]["warnings"][0].startswith("king: no image published") and "warning: king: no image" in err
    code, out, err = run(capsys, "--submission", SID, "--doc", p, "--image", "king", "--scenes", "3", "--hash-scenes", "0")
    assert code == 0 and out["rerun"] == {"role": "king", "image": None, "skipped": "no image published"}, out
    assert "king: no image published; not re-running it" in err
    doc = make_doc(fake_env(broken=BROKEN), "crowned", genesis=True)
    doc["king"]["id"] = "sub-7"
    code, out, _ = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc), "--hash-scenes", "0")
    assert code == 1 and "king: no image published" in out["document"]["problems"], out["document"]


def test_hostile_json_is_refused(capsys, env, tmp_path, monkeypatch):
    doc = make_doc(fake_env(broken=BROKEN))
    nan = json.dumps(doc).replace('"margin": 0.02', '"margin": NaN')
    for text, msg in ((nan, "NaN"), ("[" * 100000 + "]" * 100000, "not JSON"), ("[1, 2]", "not a verify document")):
        p = tmp_path / "h.json"
        p.write_text(text)
        code, out, err = run(capsys, "--submission", SID, "--doc", str(p))
        assert code == 2 and out is None and msg in err, err
    monkeypatch.setattr(verify, "MAX_DOC_BYTES", 1000)
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc))
    assert code == 2 and "larger than 1000 bytes" in err


def test_api_failures_and_bad_arguments_exit_2(monkeypatch, capsys, env, tmp_path):
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
        monkeypatch.setattr(api_mod, "make_client",
                            lambda timeout, k=kind: httpx.Client(transport=httpx.MockTransport(handler(k))))
        code, out, err = run(capsys, "--submission", SID, "--api", "http://api")
        assert code == 2 and out is None and msg in err, (kind, err)
    monkeypatch.setattr(verify, "MAX_DOC_BYTES", 64 << 20)
    p = write(tmp_path, make_doc(fake_env(broken=BROKEN)))
    for argv, msg in ((["--workers", "0"], "--workers"), (["--wall", "-1"], "--wall"), (["--scenes", "-3"], "negative"),
                      (["--salt-sha256", "abc"], "--salt-sha256"),
                      (["--save", str(tmp_path / "no" / "x.json")], "cannot write")):
        code, out, err = run(capsys, "--submission", SID, "--doc", p, *argv)
        assert code == 2 and out is None and msg in err, (argv, err)


class BrokenScenes(Scenes):
    def generate(self, seed, draw=None):
        raise RuntimeError("sim.mjs stopped: cannot open the bundle")


def test_a_broken_local_setup_is_not_a_mismatch(monkeypatch, capsys, tmp_path):
    p = write(tmp_path, make_doc(fake_env()))
    monkeypatch.setattr(verify, "load_env", lambda: SimpleNamespace(scenes=BrokenScenes(), harness=Harness(), wall_s=45.0))
    code, out, err = run(capsys, "--submission", SID, "--doc", p)
    assert code == 2 and out is None and "the scene generator does not work here" in err and "MISMATCH" not in err

    def boom():
        raise verify.VerifyError("vector-env's settings do not load: bad yaml")
    monkeypatch.setattr(verify, "load_env", boom)
    code, out, err = run(capsys, "--submission", SID, "--doc", p)
    assert code == 2 and "settings do not load" in err


def test_one_unreproducible_scene_with_a_working_generator_is_a_mismatch(monkeypatch, capsys, tmp_path):
    doc = make_doc(fake_env())
    bad = {verify.base_seed(SALT, 0, 6) + k for k in range(6)}
    monkeypatch.setattr(verify, "load_env", lambda: fake_env(broken=bad))
    code, out, _ = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc))
    assert code == 1 and [x["index"] for x in out["scenes"]["mismatched"]] == [6]
    assert "could not place" in out["scenes"]["mismatched"][0]["error"]


def test_a_scene_made_with_another_vector_env_is_unverified_not_a_mismatch(capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=BROKEN))
    doc["scenes"][4]["env"] = {**ENV, "version": "0.0.9"}
    doc["scenes"][4]["state_hash"] = "9" * 16
    doc["scenes_sha256"] = verify.scenes_digest(doc["scenes"])
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc))
    assert code == 2 and not out["ok"] and out["document"]["ok"] and out["scenes"]["mismatched"] == []
    assert [x["index"] for x in out["scenes"]["unverified"]] == [doc["scenes"][4]["index"]]
    assert "another vector-env" in err


EXCLUDED = sorted(set(subverify.INFRA) | subverify.CUT_OFF | {subverify.NOT_RUN})


def _with_sample(doc, sample):
    res, pos = doc["phase1"]["results"], {x["index"]: k for k, x in enumerate(doc["scenes"])}
    doc["phase2"].update(scenes=sample, outcome=[res["outcome"][pos[i]] for i in sample],
                         action_hash=[res["action_hash"][pos[i]] for i in sample], wall_s=[12.5 for _ in sample])
    return doc


def test_phase1_cut_offs_stay_out_of_the_phase2_sample(capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=BROKEN), "not_crowned")
    res, rows = doc["phase1"]["results"], doc["scenes"]
    pos = {x["index"]: k for k, x in enumerate(rows)}
    cut = next(i for i in doc["phase2"]["scenes"] if not res["success"][pos[i]])
    res["outcome"][pos[cut]] = "step_timeout"
    doc["king"]["results"]["outcome"][pos[cut]] = "step_timeout"
    doc.update(phase2_scenes=6, excluded_outcomes=EXCLUDED, first_step_deadline_s=10.0)
    old = list(doc["phase2"]["scenes"])
    want = _sample([i for i in pos if i != cut])
    assert cut in old and cut not in want
    code, out, _ = run(capsys, "--submission", SID, "--doc", write(tmp_path, _with_sample(doc, want)), "--hash-scenes", "0")
    assert code == 0 and out["document"]["ok"], out["document"]
    for fn, problem in ((lambda d: _with_sample(d, old), "other scenes than the sample rule picks"),
                        (lambda d: d.update(excluded_outcomes=None) or _with_sample(d, want),
                         "other scenes than the sample rule picks"),
                        (lambda d: d.update(phase2_scenes=7), "min(phase2_scenes 7"),
                        (lambda d: d.update(excluded_outcomes=EXCLUDED + ["missed"]), "leaves missed out"),
                        (lambda d: d.update(excluded_outcomes="step_timeout"), "excluded_outcomes")):
        d = copy.deepcopy(doc)
        fn(d)
        code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, d), "--hash-scenes", "0")
        got = (out or {}).get("document", {}).get("problems", []) if out else [err]
        assert code in (1, 2) and any(problem in p for p in got), (problem, got)
