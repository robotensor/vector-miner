from __future__ import annotations

import copy
import hashlib
import json
import random
import statistics
from types import SimpleNamespace

import httpx
import pytest
from conftest import ENV, SALT, Scenes, _h

from vector_miner import api as api_mod
from vector_miner.skills.bin_picking.expert import cli, subverify, verify

SUB = "ghcr.io/alice/bin-expert@sha256:" + "a" * 64
KING = "ghcr.io/kate/bin-expert@sha256:" + "c" * 64
PUBLIC = "ghcr.io/robotensor/vector-king@sha256:" + "e" * 64
SID = "sub-42"


class Harness:
    rates = {SUB: 0.8, KING: 0.4}
    same = {PUBLIC: SUB}  # the king's public image is the same image as the one the submission committed

    def run(self, expert, scenes, *, time_limit_s, wall_s, workers=1, docker=None, on_result=None):
        out = []
        cmd = expert.get("command") or []
        ref = expert.get("image") or (cmd[cmd.index("as") + 1] if "as" in cmd else "x")
        ref = self.same.get(ref, ref)
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


OTHERS = ["sub-50", "sub-51", "sub-52"]


def make_doc(e, status="ranked", count=12, bar=0.4, p1_infra=(), p2_infra=0):
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
        bar = p1["score"]
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
    new = {"ranked": ["sub-50", SID], "not_ranked": OTHERS}.get(status, [])
    return {"format": subverify.FORMAT, "skill": "bin-picking",
            "submission": {"id": SID, "hotkey": "5Alice", "uid": 4, "image": SUB, "digest": "sha256:" + "a" * 64,
                           "commit_block": 100, "round": 3, "status": status,
                           "reason": p2["reason"] if status == "disqualified" else None, "mirror_url": None,
                           "committed_at": "2026-11-23T01:00:00+00:00", "scored_at": "2026-11-23T05:00:00+00:00",
                           "finished_at": "2026-11-23T09:00:00+00:00"},
            "salt": SALT, "salt_sha256": hashlib.sha256(SALT.encode()).hexdigest(),
            "salt_committed_at": "2026-11-23T01:00:01+00:00", "phase1_started_at": "2026-11-23T01:05:00+00:00",
            "seed_rule": "...", "seed_tries": 6, "skipped": skipped, "draw": None,
            "task": {"time_limit_s": 15.0}, "wall_s": 45.0, "step_deadline_s": 2.0,
            "max_restarts": 2, "margin": 0.0, "env": dict(ENV), "scene_count": count,
            "scenes_sha256": verify.scenes_digest(rows), "scenes": rows, "phase1": p1, "phase2": p2,
            "decision": {"round": 3, "bar": bar, "rank_at": 2 if status == "ranked" else None, "new": new}}


def make_old_doc(e, status="crowned"):
    """A frozen document in the older format: a king run, a decisive re-check and the old decision."""
    doc = make_doc(e, {"crowned": "ranked", "not_crowned": "not_ranked"}.get(status, status))
    doc["submission"]["status"] = status
    del doc["submission"]["round"]
    doc["margin"] = 0.02
    doc["king"] = {"id": "sub-7", "hotkey": "5Kate", "image": KING, "digest": "sha256:" + "c" * 64, "status": "king",
                   "score": 0.3, "results": {"success": [], "t": [], "outcome": [], "action_hash": []}}
    doc["decisive"] = {"status": "passed", "scenes": [], "outcome": [], "action_hash": [], "wall_s": []}
    doc["decision"] = {"king_best": 0.4 if status != "below_king" else doc["phase1"]["score"], "king": "5Kate",
                       "phase2_ran": doc["phase2"] is not None, "decisive_ran": True, "king_score": 0.3,
                       "score": 0.8, "compared": 10, "beats_king": status == "crowned", "crowned": status == "crowned"}
    return doc


def problems(capsys, tmp_path, doc):
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc), "--hash-scenes", "0")
    return code, out["document"]["problems"], out


def test_the_round_decision_is_checked(capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=BROKEN), "ranked")
    code, got, out = problems(capsys, tmp_path, doc)
    assert code == 0 and got == [] and out["decision"] == {"round": 3, "bar": 0.4, "rank_at": 2, "new": 2,
                                                         "entered": True, "above_bar": True}, out
    for fn, want in ((_set(["decision", "rank_at"], None), "ranked, but decision.rank_at is null"),
                     (_set(["decision", "rank_at"], 3), "rank_at 3, but only 2 submissions entered"),
                     (_set(["decision", "new"], ["sub-50"]), "decision.new does not name sub-42"),
                     (_set(["decision", "new"], [SID, SID]), "decision.new repeats"),
                     (_set(["decision", "bar"], 0.99), "is not above the bar 0.99"),
                     (_set(["decision", "round"], 4), "decision.round 4 is not the submission's round 3"),
                     (_set(["decision"], None), "ranked, but the document has no decision")):
        code, got, _ = problems(capsys, tmp_path, _t(doc, fn))
        assert code == 1 and any(want in p for p in got), (want, got)
    nr = make_doc(fake_env(broken=BROKEN), "not_ranked")
    code, got, out = problems(capsys, tmp_path, nr)
    assert code == 0 and got == [] and out["decision"]["entered"] is False, out
    for fn, want in ((_set(["decision", "new"], OTHERS[:2]), "only 2 submissions entered at round 3's decision"),
                     (_set(["decision", "new"], [SID]), "not_ranked, but decision.new names this submission"),
                     (_set(["decision", "rank_at"], 1), "not_ranked, but decision.rank_at is 1")):
        code, got, _ = problems(capsys, tmp_path, _t(nr, fn))
        assert code == 1 and any(want in p for p in got), (want, got)
    low = _t(nr, lambda d: d["decision"].update(bar=d["phase1"]["score"], new=[]))
    code, got, _ = problems(capsys, tmp_path, low)
    assert code == 1 and any("phase 2 ran, but the phase-1 score" in p for p in got), got
    first = _t(doc, _set(["decision", "bar"], None))
    assert problems(capsys, tmp_path, first)[:2] == (0, [])


def test_older_documents_still_check(capsys, env, tmp_path):
    for st in ("crowned", "not_crowned", "below_king"):
        doc = make_old_doc(fake_env(broken=BROKEN), st)
        code, got, out = problems(capsys, tmp_path, doc)
        assert code == 0 and got == [] and out["decision"] is None, (st, out["document"])
        assert any("an older document" in w for w in out["document"]["warnings"]), out["document"]
    doc = make_old_doc(fake_env(broken=BROKEN), "crowned")
    code, got, _ = problems(capsys, tmp_path, _t(doc, _set(["decision", "king_best"], 0.99)))
    assert code == 1 and any("is not above the king's best 0.99 + margin 0.02" in p for p in got), got
    code, got, _ = problems(capsys, tmp_path, _t(doc, _set(["phase2", "action_hash", 0], "1" * 16)))
    assert code == 1 and any("crowned, but phase 2 did not pass" in p for p in got), got
    late = _t(doc, lambda d: (d["submission"].update(status="disqualified", reason="1 decisive scene differed"),
                              d["decision"].update(crowned=False)))
    assert problems(capsys, tmp_path, late)[:2] == (0, [])
    low = make_old_doc(fake_env(broken=BROKEN), "below_king")
    code, got, _ = problems(capsys, tmp_path, _t(low, _set(["decision", "king_best"], 0.1)))
    assert code == 1 and any("below_king, but the phase-1 score" in p for p in got), got


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
    assert not subverify.above(0.14, 0.12, 0.02) and subverify.above(0.15, 0.12, 0.02)
    assert not subverify.above(0.4, 0.4, 0) and subverify.above(0.401, 0.4, 0)


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


@pytest.mark.parametrize("status", ["ranked", "not_ranked", "below_king", "disqualified"])
def test_every_final_status_adds_up(capsys, env, tmp_path, status):
    doc = make_doc(fake_env(broken=BROKEN), status)
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc))
    assert code == 0 and out["ok"] and out["document"]["problems"] == [], out
    assert out["submission"] == SID and out["status"] == status and out["scenes"]["checked"] == doc["scene_count"]
    assert out["scenes"]["skipped_checked"] == 1 and out["scenes"]["mismatched"] == []
    assert out["decision"]["rank_at"] == (2 if status == "ranked" else None) and "king" not in out
    if status == "below_king":
        assert out["phase2"] is None and out["decision"]["above_bar"] is False
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
    code, out, err = run(capsys, "--submission", SID, "--doc", p, "--image", "submission", "--command",
                         f"x noisy as {SUB}", "--scenes", "3")
    r = out["rerun"]
    assert code == 0 and r["hash_equal"] == 0 and len(r["mismatched"]) == 3 and r["outcome_agreement"] == 1.0, r
    code, out, err = run(capsys, "--submission", SID, "--doc", p, "--image", "ghcr.io/eve/x@sha256:" + "f" * 64)
    assert code == 2 and f"is not submission {SID}'s image" in err and "'king'" in err, err
    code, out, err = run(capsys, "--submission", SID, "--doc", p, "--image", "king")
    assert code == 2 and "--image king reads the king's public image from --api" in err, err


def board(monkeypatch, doc, king):
    seen = []

    def handler(req):
        seen.append(req.url.path)
        if req.url.path == "/v1/skills/bin-picking/expert":
            return httpx.Response(200, json={"king": king, "ranks": []})
        if req.url.path == f"/v1/skills/bin-picking/expert/submissions/{SID}/verify":
            return httpx.Response(200, json=doc)
        if req.url.path == f"/v1/skills/bin-picking/expert/submissions/{SID}":
            return httpx.Response(200, json={"id": SID, "salt_sha256": doc["salt_sha256"]})
        return httpx.Response(404, json={"detail": "not found"})
    monkeypatch.setattr(api_mod, "make_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(handler)))
    return seen


def test_image_king_reruns_the_kings_public_image(monkeypatch, capsys, env):
    doc = make_doc(fake_env(broken=BROKEN))
    seen = board(monkeypatch, doc, {"submission": SID, "hotkey": "5Alice", "public_image": PUBLIC})
    code, out, err = run(capsys, "--api", "http://api", "--image", "king", "--scenes", "4", "--hash-scenes", "0")
    r = out["rerun"]
    assert code == 0 and out["submission"] == SID and r["role"] == "king" and r["ran"] == PUBLIC, out
    assert r["scenes"] == 4 and r["hash_equal"] == 4 and r["mismatched"] == []
    assert seen[0] == "/v1/skills/bin-picking/expert"
    code, out, err = run(capsys, "--submission", "sub-9", "--api", "http://api", "--image", "king")
    assert code == 2 and f"on the king's submission ({SID}), not on sub-9" in err, err


@pytest.mark.parametrize("king, why", [
    (None, "there is no king yet"),
    ({"submission": SID, "public_image": None}, "the king's public image is not published yet"),
    ({"submission": SID}, "the king's public image is not published yet"),
    ({"submission": SID, "public_image": "ghcr.io/robotensor/vector-king:king"}, "is not a pinned image"),
])
def test_image_king_without_a_public_image(monkeypatch, capsys, env, king, why):
    board(monkeypatch, make_doc(fake_env(broken=BROKEN)), king)
    code, out, err = run(capsys, "--api", "http://api", "--image", "king", "--hash-scenes", "0")
    assert code == 2 and why in err, err


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
    ("ranked", _set(["submission", "status"], "phase2"), "not final"),
    ("ranked", _set(["submission", "status"], "passed"), "not final"),
    ("ranked", _set(["salt"], "ff" * 16), "not the sha256 of the salt"),
    ("ranked", _set(["salt_committed_at"], "2026-11-23T02:00:00+00:00"), "after phase 1 started"),
    ("ranked", _set(["salt_committed_at"], None), "--allow-uncommitted"),
    ("ranked", _set(["scenes_sha256"], "0" * 64), "scenes_sha256 does not match"),
    ("ranked", _set(["phase1", "score"], 0.99), "score 0.99 is not"),
    ("ranked", _set(["decision", "bar"], 0.99), "is not above the bar 0.99"),
    ("ranked", _set(["phase2", "action_hash", 0], "1" * 16), "phase 2 did not pass"),
    ("ranked", _set(["phase2", "wall_s", 0], 99.0), "phase 2 did not pass"),
    ("ranked", _set(["decision", "new"], []), "decision.new does not name"),
    ("ranked", _set(["submission", "image"], "ghcr.io/alice/bin-expert:latest"), "not an accepted pinned image"),
    ("not_ranked", _set(["submission", "status"], "ranked"), "decision.rank_at is null"),
    ("below_king", _set(["decision", "bar"], 0.1), "below_king, but the phase-1 score"),
    ("below_king", _set(["decision", "bar"], None), "(no king) and above the minimum score 0"),
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
    doc = make_doc(fake_env(broken=BROKEN), "ranked", p1_infra=p1_infra, p2_infra=1)
    count, p1, p2 = doc["scene_count"], doc["phase1"], doc["phase2"]
    assert p1["infra"] == 2 and p1["counted"] == count - 2 and p1["score"] == p1["successes"] / (count - 2)
    assert full[0] not in p2["scenes"] and p2["outcome"][0] == "docker_unavailable"
    code, out, err = run(capsys, "--submission", SID, "--doc", write(tmp_path, doc), "--hash-scenes", "0")
    assert code == 0 and out["ok"] and out["document"]["problems"] == [], out["document"]
    assert out["phase2"]["infra"] == [p2["scenes"][0]] and out["phase2"]["compared"] == len(p2["scenes"]) - 1
    assert out["phase2"]["passed"] and "same_scenes" not in out
    old = {"phase1": p1["successes"] / count, "sample": _sample(list(pos))}
    for fn, problem in ((_set(["phase1", "score"], old["phase1"]), "2 infrastructure failures left out"),
                        (_set(["phase2", "scenes"], old["sample"]), "other scenes than the sample rule picks"),
                        (_set(["phase1", "infra"], 0), "0 infrastructure failures published, 2 in the results"),
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


def test_hostile_json_is_refused(capsys, env, tmp_path, monkeypatch):
    doc = make_doc(fake_env(broken=BROKEN))
    nan = json.dumps(doc).replace('"margin": 0.0', '"margin": NaN')
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
    doc = make_doc(fake_env(broken=BROKEN), "not_ranked")
    res, rows = doc["phase1"]["results"], doc["scenes"]
    pos = {x["index"]: k for k, x in enumerate(rows)}
    cut = next(i for i in doc["phase2"]["scenes"] if not res["success"][pos[i]])
    res["outcome"][pos[cut]] = "step_timeout"
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


FORMULA = {"formula": "pile-1", "time_weight": 0.5, "path_weight": 0.0, "path_ref_m": 3.0, "out_unit": 1.0}


def make_pile_doc(e, status="ranked", eval_score=None, weight=0.5, **kw):
    """A document of a set with a score formula: per-scene scores with the time factor and the outs penalty."""
    doc = make_doc(e, status, **kw)
    p1, lim = doc["phase1"], doc["task"]["time_limit_s"]
    res = p1["results"]
    n = len(res["success"])
    res["outs"] = [{4: 1, 8: 4}.get(k, 0) for k in range(n)]
    res["tcp_path_m"] = [round(1.0 + 0.1 * k, 3) for k in range(n)]
    res["joint_path_rad"] = [2.0] * n
    res["crate_hits"] = [0] * n
    res["target_class"] = ["top", "blocked", "buried", "hidden"] * (n // 4) + ["top"] * (n % 4)
    res["score"] = [None if not subverify._verdict(res, k) else
                    round(subverify.scene_score(bool(res["success"][k]), res["t"][k], res["outs"][k],
                                                res["tcp_path_m"][k], FORMULA, lim), 6) for k in range(n)]
    counted = [k for k in range(n) if subverify._verdict(res, k)]
    p1["score"] = sum(res["score"][k] for k in counted) / len(counted)
    p1["outs"] = sum(res["outs"][k] for k in counted)
    doc["score_formula"] = dict(FORMULA)
    doc["min_score"] = -1000.0  # a round whose floor lets negative scores rank
    if eval_score is None:
        doc["phase2_eval"] = None
        doc["final_score"] = p1["score"] if status in ("ranked", "not_ranked") else None
    else:
        doc["phase2_eval"] = {"scenes": 20, "counted": 20, "successes": 9, "outs": 3, "mean_time_s": 9.5, "infra": 0,
                              "score": eval_score, "weight": weight}
        doc["final_score"] = (1 - weight) * p1["score"] + weight * eval_score
    if status == "below_king":
        doc["decision"]["bar"] = p1["score"]
    else:
        doc["decision"]["bar"] = min(p1["score"], doc["final_score"] or p1["score"]) - 0.05
    return doc


def test_pile_scores_follow_the_formula(capsys, env, tmp_path):
    doc = make_pile_doc(fake_env(broken=BROKEN), "ranked")
    assert doc["phase1"]["score"] < 0  # outs make it negative
    code, got, out = problems(capsys, tmp_path, doc)
    assert code == 0 and got == [], got
    k = next(k for k, v in enumerate(doc["phase1"]["results"]["success"]) if v)
    for fn, want in ((_set(["phase1", "results", "score", k], 1.0), "scene scores do not follow the score formula"),
                     (_set(["phase1", "score"], 0.5), "is not the mean scene score"),
                     (_set(["phase1", "outs"], 99), "99 outs published"),
                     (_set(["phase1", "results", "outs", k], 2), "scene scores do not follow the score formula"),
                     (_set(["phase1", "results", "score"], None), "scene scores are expected"),
                     (_set(["score_formula", "formula"], "other-9"), "unknown score formula"),
                     (_set(["final_score"], 0.9), "is not the phase-1 score")):
        code, got, _ = problems(capsys, tmp_path, _t(doc, fn))
        assert code == 1 and any(want in p for p in got), (want, got)
    # a different time weight changes every successful scene's score
    code, got, _ = problems(capsys, tmp_path, _t(doc, _set(["score_formula", "time_weight"], 0.0)))
    assert code == 1 and any("do not follow the score formula" in p for p in got), got


def test_pile_final_score_with_an_evaluation_on_other_assets(capsys, env, tmp_path):
    doc = make_pile_doc(fake_env(broken=BROKEN), "ranked", eval_score=0.2)
    code, got, out = problems(capsys, tmp_path, doc)
    assert code == 0 and got == [], got
    assert any("private evaluation" in w for w in out["document"]["warnings"]), out["document"]
    code, got, _ = problems(capsys, tmp_path, _t(doc, _set(["final_score"], doc["final_score"] + 0.1)))
    assert code == 1 and any("is not (1 - 0.5)" in p for p in got), got
    p1 = doc["phase1"]["score"]
    # the round decision ranks by the final score: phase 1 above the bar, the final score below it
    worse = make_pile_doc(fake_env(broken=BROKEN), "ranked", eval_score=p1 - 2.0)
    worse["decision"]["bar"] = p1 - 0.5
    code, got, _ = problems(capsys, tmp_path, worse)
    assert code == 1 and any("ranked, but the final score" in p for p in got), got
    nr = make_pile_doc(fake_env(broken=BROKEN), "not_ranked", eval_score=p1 - 2.0)
    nr["decision"].update(bar=p1 - 0.5, new=[])
    code, got, out = problems(capsys, tmp_path, nr)
    assert code == 0 and got == [] and out["decision"]["above_bar"] is False, got
    code, got, _ = problems(capsys, tmp_path, _t(nr, _set(["final_score"], None)))
    assert code == 1 and any("has no final_score" in p for p in got), got


def test_pile_below_king_and_negative_bars(capsys, env, tmp_path):
    doc = make_pile_doc(fake_env(broken=BROKEN), "below_king")
    assert problems(capsys, tmp_path, doc)[:2] == (0, [])
    code, got, _ = problems(capsys, tmp_path, _t(doc, _set(["decision", "bar"], doc["phase1"]["score"] - 1.0)))
    assert code == 1 and any("below_king, but the phase-1 score" in p for p in got), got


def test_pile_scores_without_a_king_need_the_minimum_score(capsys, env, tmp_path):
    low = make_pile_doc(fake_env(broken=BROKEN), "below_king")
    low["decision"]["bar"] = None
    del low["min_score"]  # no min_score published: the floor is 0
    assert low["phase1"]["score"] < 0 and problems(capsys, tmp_path, low)[:2] == (0, [])
    code, got, _ = problems(capsys, tmp_path, _t(low, _set(["min_score"], -100.0)))
    assert code == 1 and any("above the minimum score -100" in p for p in got), got
    # phase 1 above the floor, the final score below it: not ranked, although nobody entered
    nr = make_pile_doc(fake_env(broken=BROKEN), "not_ranked", eval_score=-5.0)
    nr["decision"].update(bar=None, new=[])
    nr["min_score"] = (nr["phase1"]["score"] + nr["final_score"]) / 2
    code, got, out = problems(capsys, tmp_path, nr)
    assert code == 0 and got == [] and out["decision"]["above_bar"] is False, got


def make_picks_doc(e, status="ranked", picks=10):
    """Scenes of several picks in the same bin: scene score = (sum of pick scores - outs penalty) / picks."""
    doc = make_pile_doc(e, status)
    res, lim = doc["phase1"]["results"], doc["task"]["time_limit_s"]
    doc["task"]["picks"] = picks
    n = len(res["success"])
    res["picks"], res["score"], res["outs"] = [], [], []
    for k in range(n):
        row = []
        for i in range(picks - (k % 3)):  # some scenes end early: no eligible target left
            ok = (k + i) % 3 != 0
            row.append({"target": i, "target_class": "top", "outcome": "success" if ok else "missed",
                        "t": round(3.0 + i * 0.7, 3), "tick": 60 + i, "outs": 1 if (k == 4 and i == 2) else 0,
                        "out_parts": [], "tcp_path_m": 1.2, "joint_path_rad": 2.0, "crate_hits": 0})
        sc, outs = subverify.picks_score(row, FORMULA, lim, picks)
        res["picks"].append(row)
        res["score"].append(round(sc, 6) if subverify._verdict(res, k) else None)
        res["outs"].append(outs)
    counted = [k for k in range(n) if subverify._verdict(res, k)]
    doc["phase1"]["score"] = sum(res["score"][k] for k in counted) / len(counted)
    doc["phase1"]["outs"] = sum(res["outs"][k] for k in counted)
    doc["phase1"]["mean_time_s"] = statistics.fmean(pk["t"] for x in res["picks"] for pk in x if pk["outcome"] == "success")
    doc["final_score"] = doc["phase1"]["score"] if status in ("ranked", "not_ranked") else None
    doc["decision"]["bar"] = doc["phase1"]["score"] - 0.05 if status != "below_king" else doc["phase1"]["score"]
    return doc


def test_scenes_of_several_picks_are_scored_from_their_picks(capsys, env, tmp_path):
    doc = make_picks_doc(fake_env(broken=BROKEN), "ranked")
    assert 0 < doc["phase1"]["score"] < 1
    code, got, _ = problems(capsys, tmp_path, doc)
    assert code == 0 and got == [], got
    for fn, want in ((_set(["phase1", "results", "picks", 0, 0, "outcome"], "success"), "do not follow the score formula"),
                     (_set(["phase1", "results", "picks", 4, 2, "outs"], 0), "its picks 0"),
                     (_set(["task", "picks"], 5), "do not follow the score formula"),
                     (_set(["phase1", "mean_time_s"], 1.0), "or of its successful picks")):
        code, got, _ = problems(capsys, tmp_path, _t(doc, fn))
        assert code == 1 and any(want in p for p in got), (want, got)


def test_not_ranked_without_phase_2_needs_three_ahead(capsys, env, tmp_path):
    nr = _t(make_doc(fake_env(broken=BROKEN), "not_ranked"), _set(["phase2"], None))
    code, got, _ = problems(capsys, tmp_path, nr)
    assert code == 0 and got == [], got
    code, got, _ = problems(capsys, tmp_path, _t(nr, _set(["decision", "new"], OTHERS[:2])))
    assert code == 1 and any("no phase 2 is published" in p for p in got), got
    pile = make_pile_doc(fake_env(broken=BROKEN), "not_ranked")
    pile.update(phase2=None, final_score=None)
    pile["decision"]["new"] = list(OTHERS)
    code, got, _ = problems(capsys, tmp_path, pile)
    assert code == 0 and got == [], got


def test_the_phase2_sample_seed_and_the_expert_time_per_pick(capsys, env, tmp_path):
    doc = make_doc(fake_env(broken=BROKEN), "ranked")
    rows, res = doc["scenes"], doc["phase1"]["results"]
    ok = [x["index"] for x in rows]
    h = "0x" + "ab" * 32
    seed = h
    pick = sorted(sorted(ok, key=lambda i: hashlib.sha256(f"{seed}:phase2:{i}".encode()).hexdigest())[:6])
    pos = {x["index"]: k for k, x in enumerate(rows)}
    doc["phase2"].update(scenes=pick, outcome=[res["outcome"][pos[i]] for i in pick],
                         action_hash=[res["action_hash"][pos[i]] for i in pick], wall_s=[300.0] * 6,
                         pick_expert_s=[[1.0, 2.0]] * 6, sample_seed={"source": "chain", "block": 7, "hash": h,
                                                                      "seed": seed})
    doc.update(wall_s=45.0, scene_wall_s=90.0)
    code, probs, out = problems(capsys, tmp_path, doc)
    assert not [p for p in probs if "phase 2" in p or "sample" in p], probs
    assert out["phase2"]["over_wall"] == [] and out["phase2"]["passed"]
    doc["phase2"]["pick_expert_s"] = [[1.0, 50.0]] + [[1.0, 2.0]] * 5
    code, probs, out = problems(capsys, tmp_path, doc)
    assert out["phase2"]["over_wall"] == [pick[0]]
    doc["phase2"]["pick_expert_s"] = [[1.0, 2.0]] * 6
    doc["phase2"]["sample_seed"] = {**doc["phase2"]["sample_seed"], "seed": "f" * 64}
    code, probs, out = problems(capsys, tmp_path, doc)
    assert any("sample seed is not the hash of block" in p for p in probs), probs
