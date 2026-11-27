from __future__ import annotations

import json

import httpx
from conftest import HOT, REF, _api

from vector_miner import api as api_mod
from vector_miner.skills.bin_picking.expert import cli


def test_status_shows_progress_queue_phase2_rank_and_verdict(monkeypatch, capsys):
    rows = {HOT: [{"id": "sub-3", "hotkey": HOT, "image": REF, "status": "scored", "committed_at": "2026-11-23T01:00:00Z",
                   "salt_sha256": "c" * 64, "phase1": {"done": 1000, "total": 1000, "score": 0.71}, "queue_position": 2}]}
    ex = {"king": None, "ranks": [], "bar": None, "margin": 0, "rank_shares": [0.7, 0.2, 0.1], "decisions": []}

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/v1/skills/bin-picking/expert":
            return httpx.Response(200, json=ex)
        if p == "/v1/skills/bin-picking/expert/submissions":
            return httpx.Response(200, json={"submissions": rows.get(req.url.params["hotkey"], [])})
        return httpx.Response(404)

    _api(monkeypatch, handler)
    assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    s = out["submissions"][0]
    assert out["king"] is False and out["bar"] is None and out["ranks"] == [] and out["rank"] is None
    assert "no ranks yet" in out["ranks_note"] and "vector-miner: no ranks yet" in cap.err
    assert "no king yet, so no bar" in s["verdict"]
    pub = "ghcr.io/robotensor/vector-king@sha256:" + "e" * 64
    king = {"submission": "sub-1", "hotkey": "5King", "image": REF, "score": 0.64, "since_round": 2,
            "public_image": pub}
    ex.update(king=king, bar=0.64, ranks=[{"rank": 1, "share": 0.7, "hotkey": "5King", "score": 0.64, "round": 2},
                                          {"rank": 2, "share": 0.2, "hotkey": "5Two", "score": 0.5, "round": 2}],
              decisions=[{"round": 3, "bar": 0.66, "new": ["sub-9"], "ranks": []}])
    assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    s = out["submissions"][0]
    assert out["king_hotkey"] == "5King" and out["king_score"] == 0.64 and out["bar"] == 0.64 and out["margin"] == 0
    assert [r["rank"] for r in out["ranks"]] == [1, 2] and "ranks_note" not in out
    assert s["phase1"] == {"done": 1000, "total": 1000, "score": 0.71, "successes": None} and s["queue_position"] == 2
    assert "above the bar +0.640" in s["verdict"] and "evaluation queue position 2" in s["verdict"]
    assert "sub-3: phase 1 done: +0.710" in cap.err
    rows[HOT][0].update(round=3, evaluates_at="2026-11-30T00:00:00+00:00")
    assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 0
    s = json.loads(capsys.readouterr().out)["submissions"][0]
    assert s["round"] == 3 and "round 3 ends at 2026-11-30T00:00:00+00:00, then its evaluation (the copy, phase 2)" in s["verdict"]
    views = {
        "waiting_connection": ({}, "run vector-miner serve"),
        "phase1": ({"phase1": {"done": 412, "total": 1000}}, "412/1000 scenes"),
        "below_king": ({"phase1": {"score": 0.6}}, "not above the bar +0.640"),
        "phase2": ({}, "every action hash must equal phase 1's"),
        "passed": ({"round": 3}, "phase 2 passed · decided when round 3 is"),
        "ranked": ({"rank": 1, "rank_at": 1}, "rank 1 · 70% of bin-picking-expert's emission (the king)"),
        "not_ranked": ({"round": 3, "phase1": {"score": 0.7}}, "not in round 3's top three"),
        "crowned": ({}, "crowned (an older status"),
        "not_crowned": ({}, "not crowned (an older status)"),
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
        assert s["final"] == (st in ("below_king", "ranked", "not_ranked", "crowned", "not_crowned", "disqualified",
                                     "abandoned", "invalid", "copy"))
        if st == "review":
            assert s["retry_at"] == "2026-11-23T16:00:00+00:00"
    for st, extra, want in (("phase2", {"stage": "copy"}, "we copy your committed image into our registry first"),
                            ("ranked", {"rank": 3}, "rank 3 · 10% of bin-picking-expert's emission"),
                            ("ranked", {"rank": None, "rank_at": 2, "ranked_out_round": 5},
                             "ranked out in round 5 (entered at rank 2); final"),
                            ("not_ranked", {"round": 3, "phase1": {"score": 0.65}}, "final score +0.650 not above the bar +0.660"),
                            ("not_ranked", {"round": 1, "phase1": {"score": 0.65}}, "not above the king, or not in"),
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
    king["hotkey"] = HOT
    ex["ranks"][0]["hotkey"] = HOT
    rows[HOT] = []
    assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["king_image"] == pub and out["rank"] == 1
    del king["public_image"]
    assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 0
    assert json.loads(capsys.readouterr().out)["king_image"] is None
    assert out["king"] is True and out["submissions"] == [] and "vector-miner commit" in out["note"]


def test_status_unreachable_api_is_one_line(monkeypatch, capsys):
    def handler(req):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(api_mod, "make_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(handler)))
    assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 2
    assert capsys.readouterr().err.startswith("vector-miner: cannot reach http://api")


def test_the_unpaid_verdict():
    from vector_miner.skills.bin_picking.expert import board
    v = board.verdict({"status": "unpaid", "reason": "No fee payment of 0.1 TAO"}, {})
    assert v.startswith("unpaid: No fee payment") and "vector-miner fee pay" in v and "commit again" in v
    assert board.existing([{"status": "unpaid", "commit_block": 1}]) is None


def test_status_shows_the_phase1_slot():
    from vector_miner.skills.bin_picking.expert import board
    row = {"status": "phase1", "phase1": {"done": 40, "total": 1000, "slots": 2, "slots_max": 3}}
    assert "40/1000 scenes" in board.verdict(row, {}) and "slot 2 of 3" in board.verdict(row, {})
    assert "slot" not in board.verdict({"status": "phase1", "phase1": {"done": 40, "total": 1000}}, {})


def _leaf_view(leaf):
    ex = {"king": None, "ranks": [], "bar": None, "margin": 0, "rank_shares": [0.7, 0.2, 0.1], "decisions": []}
    if leaf is not None:
        ex["leaf"] = leaf

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/skills/bin-picking/expert":
            return httpx.Response(200, json=ex)
        if req.url.path == "/v1/skills/bin-picking/expert/submissions":
            return httpx.Response(200, json={"submissions": []})
        return httpx.Response(404)
    return handler


ROUND = {"n": 2, "starts_at": "2026-11-24T00:00:00+00:00", "ends_at": "2026-12-01T00:00:00+00:00"}
NEXT = {"n": 3, "starts_at": "2026-12-01T00:00:00+00:00", "ends_at": "2026-12-08T00:00:00+00:00"}


def test_status_shows_the_leaf_and_says_when_it_plays_no_phase1(monkeypatch, capsys):
    full = {"id": "bin-picking-expert", "skill": "bin-picking", "kind": "expert", "name": "Expert", "status": "ongoing",
            "round": ROUND, "next": None, "final_round": None, "round_mode": "auto", "interval_s": 604800, "fee": None}
    _api(monkeypatch, _leaf_view(full))
    assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["leaf"] == {"id": "bin-picking-expert", "status": "ongoing", "round": ROUND, "next": None}
    assert "plays no phase 1" not in cap.err
    for status, nxt, line in (
            ("planned", NEXT, "vector-miner: bin-picking-expert is planned: it plays no phase 1 now; its next round "
                              "starts at 2026-12-01T00:00:00+00:00\n"),
            ("paused", None, "vector-miner: bin-picking-expert is paused: it plays no phase 1 now\n"),
            ("finished", None, "vector-miner: bin-picking-expert is finished: it plays no phase 1 now\n")):
        _api(monkeypatch, _leaf_view({**full, "status": status, "round": None, "next": nxt}))
        assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 0
        cap = capsys.readouterr()
        assert json.loads(cap.out)["leaf"] == {"id": "bin-picking-expert", "status": status, "round": None, "next": nxt}
        assert line in cap.err, cap.err
    _api(monkeypatch, _leaf_view(None))
    assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["leaf"] is None and "plays no phase 1" not in cap.err


def test_a_hidden_competition_is_an_api_error(monkeypatch, capsys):
    _api(monkeypatch, lambda req: httpx.Response(404, json={"detail": "no such competition"}))
    assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 2
    assert capsys.readouterr().err == "vector-miner: http://api: HTTP 404: no such competition\n"


def test_status_shows_signed_scores_final_eval_outs_and_mean_time(monkeypatch, capsys):
    row = {"id": "sub-5", "hotkey": HOT, "image": REF, "status": "below_king",
           "phase1": {"done": 1000, "total": 1000, "score": -1.25, "successes": 600, "outs": 410, "mean_time_s": 12.34}}
    ex = {"king": {"hotkey": "5King", "score": -0.5}, "ranks": [], "bar": -0.5, "margin": 0,
          "rank_shares": [0.7, 0.2, 0.1], "decisions": [{"round": 3, "bar": -0.5, "new": [], "ranks": []}],
          "phase2_eval": {"enabled": True, "scenes": 200, "weight": 0.5}}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/skills/bin-picking/expert":
            return httpx.Response(200, json=ex)
        if req.url.path == "/v1/skills/bin-picking/expert/submissions":
            return httpx.Response(200, json={"submissions": [row]})
        return httpx.Response(404)

    _api(monkeypatch, handler)
    assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 0
    out = json.loads(capsys.readouterr().out)
    s = out["submissions"][0]
    assert out["bar"] == -0.5 and out["phase2_eval"] == {"enabled": True, "scenes": 200, "weight": 0.5}
    assert s["phase1"]["outs"] == 410 and s["phase1"]["mean_time_s"] == 12.34
    assert ("phase 1 scored -1.250 (600/1000 successes, 410 out of the bin, mean time 12.3 s), not above the bar -0.500"
            in s["verdict"]), s["verdict"]
    row.update(status="phase2", phase2_eval={"done": 40, "total": 200, "score": None, "weight": 0.5})
    assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 0
    s = json.loads(capsys.readouterr().out)["submissions"][0]
    assert "the evaluation on other assets: 40/200 scenes" in s["verdict"] and s["phase2_eval"]["total"] == 200
    row.update(status="passed", round=3, eval_score=0.1, final_score=-0.575)
    assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 0
    s = json.loads(capsys.readouterr().out)["submissions"][0]
    assert "phase 2 passed · final score -0.575" in s["verdict"] and s["final_score"] == -0.575 and s["eval_score"] == 0.1
    row.update(status="not_ranked")
    assert cli.main(["status", "--api", "http://api", "--hotkey", HOT]) == 0
    s = json.loads(capsys.readouterr().out)["submissions"][0]
    assert "not ranked: final score -0.575 not above the bar -0.500; final" in s["verdict"], s["verdict"]


def test_status_shows_phase1_progress_and_the_claimed_score():
    from vector_miner.skills.bin_picking.expert import board
    v = board.verdict({"status": "phase1", "phase1": {"done": 12, "total": 100, "score": -0.25}}, {})
    assert v == ("phase 1: 12/100 scenes answered by your miner (run on your machine); claimed score so far -0.250 "
                 "(phase 2 re-runs a sample); keep serve running")
    assert "claimed" not in board.verdict({"status": "phase1", "phase1": {"done": 0, "total": 100}}, {})
