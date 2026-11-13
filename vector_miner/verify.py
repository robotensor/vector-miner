from __future__ import annotations

import hashlib
import random
import statistics
import sys
from types import SimpleNamespace

FORMAT = "vector-round-verify/1"


class VerifyError(Exception):
    pass


def load_env() -> SimpleNamespace:
    try:
        from vector_env import harness, scenes
        from vector_env.settings import env_config
    except ImportError:
        raise VerifyError("regenerating scenes needs vector-env (scripts/setup.sh installs it)") from None
    return SimpleNamespace(scenes=scenes, harness=harness, wall_s=float(env_config()["harness"]["wall_s"]))


def _log(*x) -> None:
    print(*x, file=sys.stderr, flush=True)


def base_seed(salt: str, n: int, index: int) -> int:
    return int.from_bytes(hashlib.sha256(f"{salt}:{int(n)}:{int(index)}".encode()).digest()[:6], "big")


def scenes_digest(rows: list[dict]) -> str:
    return hashlib.sha256("\n".join(f"{x['index']}:{x['seed']}:{x['state_hash']}" for x in rows).encode()).hexdigest()


def _rank_key(x: dict) -> tuple:
    return (-(x.get("score") or 0.0), x["mean_time_s"] if x.get("mean_time_s") is not None else 1e9,
            x.get("commit_block") or 0)


def label(im: dict) -> str:
    return im.get("run_image") or im.get("image") or f"{im.get('role')} {im.get('hotkey')}"


def check_document(doc: dict) -> list[str]:
    if doc.get("format") != FORMAT:
        return [f"unknown document format {doc.get('format')!r} (this vector-miner reads {FORMAT})"]
    bad = []
    n, salt, rows = int(doc["round"]), str(doc["salt"]), doc["scenes"]
    count, tries = int(doc["scene_count"]), int(doc.get("seed_tries") or 6)
    if doc.get("status") != "final":
        bad.append(f"the round is {doc.get('status')}, not final")
    if len(rows) != count or sorted(x["index"] for x in rows) != list(range(count)):
        bad.append(f"the scene list does not hold indexes 0..{count - 1}")
    if scenes_digest(rows) != doc.get("scenes_sha256"):
        bad.append("scenes_sha256 does not match the scene list")
    for x in rows:
        if not 0 <= int(x["seed"]) - base_seed(salt, n, x["index"]) < tries:
            bad.append(f"scene {x['index']}: seed {x['seed']} does not derive from the salt")
    images = doc.get("images") or []
    for im in images:
        res, name = im["results"], label(im)
        if any(len(res[k]) != count for k in ("success", "t", "outcome")):
            bad.append(f"{name}: {count} results expected")
            continue
        ok = sum(1 for v in res["success"] if v)
        if ok != im.get("successes"):
            bad.append(f"{name}: {im.get('successes')} successes published, {ok} in the results")
        if im.get("score") is None or abs(float(im["score"]) - ok / max(1, count)) > 1e-9:
            bad.append(f"{name}: score {im.get('score')} is not {ok}/{count}")
        ts = [float(t) for v, t in zip(res["success"], res["t"]) if v and t is not None]
        if ts and im.get("mean_time_s") is not None and abs(statistics.fmean(ts) - float(im["mean_time_s"])) > 1e-3:
            bad.append(f"{name}: mean time {im['mean_time_s']} is not the mean of its successful scenes")
    ch = sorted((x for x in images if x.get("role") == "challenger" and x.get("rank")), key=lambda x: x["rank"])
    if any(_rank_key(a) > _rank_key(b) for a, b in zip(ch, ch[1:])):
        bad.append("the challenger ranks do not follow score, then mean time, then commit block")
    dec = doc.get("decision") or {}
    king = next((x for x in images if x.get("role") == "king"), None)
    if king is not None and king.get("score") != dec.get("king_score"):
        bad.append("the king's score is not the decision's king_score")
    best = ch[0] if ch else None
    if best is not None and not best.get("winner"):
        bad.append("the top-ranked challenger is not marked as the winner")
    beats = (best is not None and (best.get("score") or 0) > 0
             and float(best.get("score") or 0) > float(dec.get("king_score") or 0.0) + float(doc.get("margin") or 0))
    if beats != bool(dec.get("beats_king")):
        bad.append(f"beats_king should be {beats}")
    if dec.get("crowned") and not beats:
        bad.append("a challenger was crowned without beating the king by the margin")
    if bool(dec.get("crowned")) != any(x.get("crowned") for x in images):
        bad.append("the crowned flag of the decision and of the images disagree")
    return bad


def sample_order(count: int, seed: int) -> list[int]:
    return random.Random(int(seed)).sample(range(count), count)


def regenerate(doc: dict, e: SimpleNamespace, indexes: list[int], log=_log) -> dict:
    n, salt, draw = int(doc["round"]), str(doc["salt"]), doc.get("draw")
    tries = int(doc.get("seed_tries") or 6)
    rows = {x["index"]: x for x in doc["scenes"]}
    made, bad = {}, []
    for j, i in enumerate(indexes):
        x, sc, why = rows[i], None, None
        for k in range(tries):
            try:
                sc = e.scenes.generate(base_seed(salt, n, i) + k, draw)
                break
            except Exception as ex:
                why = str(ex)
        if sc is None:
            bad.append({"index": i, "seed": x["seed"], "state_hash": x["state_hash"], "error": why})
        elif int(sc["seed"]) != int(x["seed"]) or sc["state_hash"] != x["state_hash"]:
            bad.append({"index": i, "seed": x["seed"], "state_hash": x["state_hash"],
                        "got_seed": int(sc["seed"]), "got_state_hash": sc["state_hash"]})
        else:
            made[i] = sc
        log(f"scene {j + 1}/{len(indexes)}: index {i} " + ("ok" if i in made else "MISMATCH"))
    return {"checked": len(indexes), "matched": len(made), "mismatched": bad, "scenes": made}


def find_image(doc: dict, ref: str) -> dict:
    ref = ref.strip()
    for im in doc.get("images") or []:
        keys = {im.get("image"), im.get("run_image"), im.get("digest"), im.get("hotkey")}
        if ref in keys or (im.get("digest") and ref.endswith("@" + im["digest"])):
            return im
    have = ", ".join(label(x) for x in doc.get("images") or []) or "none"
    raise VerifyError(f"{ref} was not evaluated in round {doc['round']} (evaluated: {have})")


def rerun(doc: dict, im: dict, expert: dict, scenes: dict, e: SimpleNamespace, *, wall_s: float | None = None,
          workers: int = 1, docker: dict | None = None, log=_log) -> dict:
    idxs = list(scenes)
    pos = {x["index"]: k for k, x in enumerate(doc["scenes"])}
    res = im["results"]

    def show(j, r):
        log(f"run {j + 1}/{len(idxs)}: {r['scene_id']} {r['outcome']} t={r['t']}")

    got = e.harness.run(expert, [scenes[i] for i in idxs], time_limit_s=float(doc["task"]["time_limit_s"]),
                        wall_s=float(wall_s or doc.get("wall_s") or e.wall_s), workers=int(workers), docker=docker,
                        on_result=show)
    rows, same_ok, same_out, dts = [], 0, 0, []
    for i, r in zip(idxs, got):
        k = pos[i]
        pub = {"success": bool(res["success"][k]), "t": res["t"][k], "outcome": res["outcome"][k]}
        loc = {"success": bool(r.get("success")), "t": r.get("t"), "outcome": r.get("outcome")}
        same_ok += pub["success"] == loc["success"]
        same_out += pub["outcome"] == loc["outcome"]
        if pub["success"] and loc["success"] and pub["t"] is not None and loc["t"] is not None:
            dts.append(abs(float(pub["t"]) - float(loc["t"])))
        if pub["success"] != loc["success"] or pub["outcome"] != loc["outcome"]:
            rows.append({"index": i, "published": pub, "local": loc})
    k = max(1, len(idxs))
    return {"image": label(im), "digest": im.get("digest"), "ran": expert.get("image") or "command",
            "scenes": len(idxs), "agreement": round(same_ok / k, 4), "outcome_agreement": round(same_out / k, 4),
            "published_successes": sum(1 for i in idxs if res["success"][pos[i]]),
            "local_successes": sum(1 for r in got if r.get("success")),
            "mean_abs_dt_s": round(statistics.fmean(dts), 3) if dts else None, "disagreements": rows}


def verify(doc: dict, *, e: SimpleNamespace | None = None, hash_scenes: int | None = None, image: str | None = None,
           expert: dict | None = None, rerun_scenes: int = 20, sample_seed: int = 0, wall_s: float | None = None,
           workers: int = 1, docker: dict | None = None, log=_log) -> dict:
    problems = check_document(doc)
    out = {"round": doc.get("round"), "salt": doc.get("salt"), "scene_count": doc.get("scene_count"),
           "document": {"ok": not problems, "problems": problems}}
    if problems and doc.get("format") != FORMAT:
        out["ok"] = False
        return out
    im = find_image(doc, image) if image else None
    if im is not None and expert is None:
        if not im.get("run_image"):
            raise VerifyError(f"{label(im)} ran a program that is not published; give --command to run your own")
        expert = {"image": im["run_image"]}
    e = e or load_env()
    local = e.scenes.env_info()
    pub = doc.get("env") or {}
    match = all(local.get(k) == pub.get(k) for k in ("version", "assets_sha256") if pub.get(k) is not None)
    out["env"] = {"published": pub, "local": local, "match": match}
    if not match:
        log(f"warning: this vector-env ({local.get('version')}, assets {str(local.get('assets_sha256'))[:12]}) is not "
            f"the round's ({pub.get('version')}, assets {str(pub.get('assets_sha256'))[:12]}); state hashes can differ")
    count = int(doc["scene_count"])
    order = sample_order(count, sample_seed)
    k = min(count, max(0, int(rerun_scenes))) if im is not None else 0
    m = count if hash_scenes is None else min(count, max(k, int(hash_scenes)))
    g = regenerate(doc, e, sorted(order[:m]), log=log)
    made = g.pop("scenes")
    out["scenes"] = g
    if im is not None:
        pick = {i: made[i] for i in sorted(order[:k]) if i in made}
        out["rerun"] = rerun(doc, im, expert, pick, e, wall_s=wall_s, workers=workers, docker=docker, log=log)
    out["ok"] = not problems and not g["mismatched"]
    return out
