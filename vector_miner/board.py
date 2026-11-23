from __future__ import annotations

FINAL = ("below_king", "crowned", "not_crowned", "disqualified", "duplicate_hotkey", "invalid", "copy", "abandoned")
REFUSED = ("duplicate_hotkey",)


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _pct(v) -> str:
    return "?" if _num(v) is None else f"{100 * float(v):.1f} %"


def phase1(s: dict) -> dict:
    p = s.get("phase1") if isinstance(s.get("phase1"), dict) else {}
    return {"done": _num(p.get("done")), "total": _num(p.get("total")), "score": _num(p.get("score", s.get("score"))),
            "successes": _num(p.get("successes"))}


def king_same(s: dict) -> dict | None:
    k = s.get("king_run") if isinstance(s.get("king_run"), dict) else None
    if k is None:
        return None
    return {"hotkey": k.get("hotkey"), "image": k.get("image"), "done": _num(k.get("done")),
            "total": _num(k.get("total")), "score": _num(k.get("score"))}


def verdict(s: dict, king: dict, margin: float | None) -> str:
    st, p1 = s.get("status"), phase1(s)
    best, m = _num(king.get("best_score", king.get("score"))), margin if margin is not None else 0.02
    kr, why = king_same(s) or {}, s.get("reason")
    if st == "waiting_connection":
        return ("waiting for your miner: run vector-miner serve --image <your image> so phase 1 can start "
                "(abandoned after 48 h without a connection)")
    if st == "phase1":
        return f"phase 1: {p1['done'] or 0}/{p1['total'] or 1000} scenes played against your miner; keep serve running"
    if st == "scored":
        q = s.get("queue_position")
        line = f"phase 1 done: {_pct(p1['score'])}"
        if best is not None:
            line += f"; it beats the king's best {_pct(best)} by more than the margin {m:g}"
        return line + (f"; phase 2 queue position {q}" if q is not None else "; waiting for phase 2")
    if st == "below_king":
        return (f"phase 1 scored {_pct(p1['score'])}, not above the king's best {_pct(best)} + margin {m:g}: "
                "no phase 2, final")
    if st == "phase2":
        return "phase 2: we run your committed image in docker (--network none) on 100 of your scenes; every action hash must equal phase 1's"
    if st == "king_run":
        return f"phase 2 passed; the king's image is running on your scenes ({kr.get('done') or 0}/{kr.get('total') or p1['total'] or 1000})"
    if st == "crowned":
        return f"crowned: {_pct(p1['score'])} against the king's {_pct(kr.get('score'))} on the same scenes"
    if st == "not_crowned":
        return (f"not crowned: {_pct(p1['score'])} is not above the king's {_pct(kr.get('score'))} + margin {m:g} on "
                "the same scenes; final")
    if st == "disqualified":
        return f"disqualified in phase 2: {why or 'no reason given'}; final"
    if st == "duplicate_hotkey":
        return "refused: this hotkey already had a submission (one per hotkey, ever)"
    if st == "invalid":
        return f"invalid: {why or 'no reason given'}"
    if st == "copy":
        return f"a copy of an earlier submission: {why or 'the same image digest'}"
    if st == "abandoned":
        return "abandoned: no miner connection within 48 h of the commit; final"
    return f"status {st}"


def view(s: dict, king: dict, margin: float | None) -> dict:
    return {"id": s.get("id"), "image": s.get("image"), "status": s.get("status"),
            "committed_at": s.get("committed_at"), "salt_sha256": s.get("salt_sha256"), "phase1": phase1(s),
            "queue_position": s.get("queue_position"), "phase2": s.get("phase2"), "king_same_scenes": king_same(s),
            "reason": s.get("reason"), "final": s.get("status") in FINAL, "verdict": verdict(s, king, margin)}


def status(api, hotkey: str) -> dict:
    ex = api.expert()
    king = ex.get("king") if isinstance(ex.get("king"), dict) else {}
    margin = _num(ex.get("margin"))
    subs = [view(s, king, margin) for s in api.submissions(hotkey)]
    out = {"hotkey": hotkey, "king": king.get("hotkey") == hotkey, "king_hotkey": king.get("hotkey"),
           "king_best": _num(king.get("best_score", king.get("score"))), "margin": margin, "submissions": subs}
    if not subs:
        out["note"] = "no submission from this hotkey yet: vector-miner commit --image <image@sha256:...>, then vector-miner serve"
    return out


def existing(rows: list[dict]) -> dict | None:
    keep = [x for x in rows if x.get("status") not in REFUSED] or rows
    return keep[0] if keep else None


def refusal(hotkey: str, s: dict) -> str:
    return (f"vector-miner: hotkey {hotkey} already has submission {s.get('id')} ({s.get('image')}, "
            f"status {s.get('status')}, committed {s.get('committed_at')}). The subnet takes one submission per "
            "hotkey, ever: another commit from this hotkey would be refused as duplicate_hotkey and never evaluated. "
            "To submit another image, register a new hotkey (btcli subnet register) and commit from it; "
            "vector-miner status shows how this one is doing")
