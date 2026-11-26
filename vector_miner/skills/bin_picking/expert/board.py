from __future__ import annotations

from . import ID

FINAL = ("below_king", "ranked", "not_ranked", "disqualified", "duplicate_hotkey", "invalid", "copy", "abandoned",
         "unpaid", "crowned", "not_crowned")
NO_SLOT = ("duplicate_hotkey", "invalid", "copy", "abandoned", "unpaid")
SHARES = (0.7, 0.2, 0.1)


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _pct(v) -> str:
    return "?" if _num(v) is None else f"{100 * float(v):.1f} %"


def _f3(v) -> str:
    return "?" if _num(v) is None else f"{float(v):.3f}"


def phase1(s: dict) -> dict:
    p = s.get("phase1") if isinstance(s.get("phase1"), dict) else {}
    out = {"done": _num(p.get("done")), "total": _num(p.get("total")), "score": _num(p.get("score", s.get("score"))),
           "successes": _num(p.get("successes"))}
    for k in ("slots", "slots_max"):
        if _num(p.get(k)) is not None:
            out[k] = _num(p.get(k))
    return out


def _share(rank, shares) -> str:
    k = rank - 1 if isinstance(rank, int) and not isinstance(rank, bool) else -1
    return f"{100 * float(shares[k]):g}%" if 0 <= k < len(shares) and _num(shares[k]) is not None else "?"


def _decided(ex: dict, rnd) -> dict | None:
    for d in ex.get("decisions") or []:
        if isinstance(d, dict) and rnd is not None and d.get("round") == rnd:
            return d
    return None


def verdict(s: dict, ex: dict) -> str:
    st, p1, rnd = s.get("status"), phase1(s), s.get("round")
    bar, why = _num(ex.get("bar")), s.get("reason")
    shares = ex.get("rank_shares") if isinstance(ex.get("rank_shares"), list) else list(SHARES)
    if st == "waiting_connection":
        return ("waiting for your miner: run vector-miner serve --image <your image> so phase 1 can start "
                "(abandoned after 48 h without a connection)")
    if st == "phase1":
        slot = f"; slot {p1['slots']:g} of {p1['slots_max']:g}" if p1.get("slots") and p1.get("slots_max") else ""
        return (f"phase 1: {p1['done'] or 0}/{p1['total'] or 1000} scenes played against your miner{slot}; "
                "keep serve running")
    if st == "scored":
        q, at = s.get("queue_position"), s.get("evaluates_at")
        line = f"phase 1 done: {_pct(p1['score'])}"
        line += f"; above the bar {_pct(bar)} (the king's score + margin)" if bar is not None else "; no king yet, so no bar"
        if at:
            return line + (f"; round {rnd} ends at {at}, then its evaluation (the copy, phase 2) starts"
                           + (f"; evaluation order {q}" if q is not None else ""))
        return line + (f"; evaluation queue position {q}" if q is not None else "; waiting for its evaluation")
    if st == "below_king":
        return (f"phase 1 scored {_pct(p1['score'])}, not above the bar {_pct(bar)} (the king's score + margin): "
                "no phase 2, final")
    if st == "phase2" and s.get("stage") == "copy":
        return ("phase 2 starts: we copy your committed image into our registry first (keep it pullable until then: "
                "public, or shared with the team account; a deleted image is disqualified)")
    if st == "phase2":
        return "phase 2: we run your committed image in docker (--network none) on 100 of your scenes; every action hash must equal phase 1's"
    if st == "passed":
        return (f"phase 2 passed · decided when round {rnd if rnd is not None else '?'} is (once every submission of "
                "that round is final or passed: the best three above the bar take ranks 1-3)")
    if st == "ranked":
        now, out = s.get("rank"), s.get("ranked_out_round")
        if isinstance(now, int) and not isinstance(now, bool):
            return f"rank {now} · {_share(now, shares)} of {ID}'s emission" + (" (the king)" if now == 1 else "")
        if out is not None:
            at = s.get("rank_at")
            return f"ranked out in round {out}" + (f" (entered at rank {at})" if at is not None else "") + "; final"
        return "ranked"
    if st == "not_ranked":
        d = _decided(ex, rnd)
        b = _num(d.get("bar")) if d else None
        if d is not None and b is not None and p1["score"] is not None and not float(p1["score"]) > float(b):
            return f"not ranked: {_f3(p1['score'])} not above king {_f3(b)}; final"
        if d is not None:
            return f"not ranked: not in round {rnd}'s top three; final"
        return (f"not ranked at round {rnd if rnd is not None else '?'}'s decision (not above the king, or not in the "
                "round's top three); final")
    if st == "crowned":
        return "crowned (an older status: ranks replaced crowns); final"
    if st == "not_crowned":
        return "not crowned (an older status); final"
    if st == "king_run":
        return "king run (an older status; no longer used)"
    if st == "disqualified":
        return f"disqualified: {why or 'no reason given'}; final"
    if st == "duplicate_hotkey":
        return "refused: this hotkey already had a submission (one per hotkey, ever)"
    if st == "invalid":
        return (f"invalid: {why or 'no reason given'}; it did not use this hotkey's submission, the hotkey may "
                "commit again")
    if st == "copy":
        return (f"a copy of an earlier submission: {why or 'the same image digest'}; it did not use this hotkey's "
                "submission, the hotkey may commit again")
    if st == "unpaid":
        return (f"unpaid: {why or 'no fee payment backed it'}; it is never evaluated and did not use this hotkey's "
                "submission: pay the fee (vector-miner fee pay), then commit again")
    if st == "review":
        at = s.get("retry_at")
        line = "review: being re-run because of a problem on our side, not your expert's"
        line += f"; it runs again at {at}" if at else "; it runs again automatically"
        if not s.get("phase2"):
            line += "; keep vector-miner serve running so phase 1 can finish"
        return line
    if st == "abandoned":
        return ("abandoned: no miner connection within 48 h of the commit; it did not use this hotkey's submission, "
                "the hotkey may commit again")
    return f"status {st}"


def leaf(ex: dict) -> dict | None:
    """The leaf's id, status, round and next round from the leaf view's `leaf` object; None without it."""
    lf = ex.get("leaf") if isinstance(ex, dict) else None
    if not isinstance(lf, dict):
        return None
    return {k: lf.get(k) for k in ("id", "status", "round", "next")}


def leaf_note(lf: dict | None) -> str | None:
    """The line `status` prints when the leaf plays no phase 1 now; None while it is ongoing or unknown."""
    if not lf or not lf.get("status") or lf.get("status") == "ongoing":
        return None
    line = f"{ID} is {lf['status']}: it plays no phase 1 now"
    nxt = lf.get("next") if isinstance(lf.get("next"), dict) else None
    if nxt and nxt.get("starts_at"):
        line += f"; its next round starts at {nxt['starts_at']}"
    return line


def view(s: dict, ex: dict) -> dict:
    return {"id": s.get("id"), "image": s.get("image"), "status": s.get("status"),
            "committed_at": s.get("committed_at"), "salt_sha256": s.get("salt_sha256"), "phase1": phase1(s),
            "round": s.get("round"), "evaluates_at": s.get("evaluates_at"),
            "queue_position": s.get("queue_position"), "phase2": s.get("phase2"), "stage": s.get("stage"),
            "rank": s.get("rank"), "rank_at": s.get("rank_at"), "ranked_out_round": s.get("ranked_out_round"),
            "reason": s.get("reason"), "retry_at": s.get("retry_at"), "final": s.get("status") in FINAL,
            "verdict": verdict(s, ex)}


def status(api, hotkey: str) -> dict:
    ex = api.expert()
    king = ex.get("king") if isinstance(ex.get("king"), dict) else {}
    ranks = [r for r in (ex.get("ranks") or []) if isinstance(r, dict)]
    subs = [view(s, ex) for s in api.submissions(hotkey)]
    mine = [r.get("rank") for r in ranks if r.get("hotkey") == hotkey]
    out = {"hotkey": hotkey, "leaf": leaf(ex), "king": bool(king) and king.get("hotkey") == hotkey, "rank": mine[0] if mine else None,
           "king_hotkey": king.get("hotkey"), "king_image": king.get("public_image"), "king_score": _num(king.get("score")),
           "bar": _num(ex.get("bar")), "margin": _num(ex.get("margin")),
           "fee": ex.get("fee") if isinstance(ex.get("fee"), dict) else None,
           "ranks": [{k: r.get(k) for k in ("rank", "share", "hotkey", "score", "round")} for r in ranks],
           "submissions": subs}
    if not ranks:
        out["ranks_note"] = "no ranks yet: the first decided round fills ranks 1-3, and every score counts until then"
    if not subs:
        out["note"] = "no submission from this hotkey yet: vector-miner commit --image <image@sha256:...>, then vector-miner serve"
    return out


def _block(x: dict) -> int:
    b = x.get("commit_block")
    return b if isinstance(b, int) and not isinstance(b, bool) else 1 << 62


def existing(rows: list[dict]) -> dict | None:
    keep = [x for x in rows if x.get("status") not in NO_SLOT]
    return min(keep, key=_block) if keep else None


def refusal(hotkey: str, s: dict) -> str:
    return (f"vector-miner: hotkey {hotkey} already has submission {s.get('id')} ({s.get('image')}, "
            f"status {s.get('status')}, committed {s.get('committed_at')}). The subnet takes one submission per "
            f"hotkey, ever, and a submission in {s.get('status')} uses it (only invalid, copy, duplicate_hotkey, "
            "abandoned and unpaid commits leave it free): another commit from this hotkey would be refused as duplicate_hotkey and never "
            "evaluated. To submit another image, register a new hotkey (btcli subnet register) and commit from it; "
            "vector-miner status shows how this one is doing")
