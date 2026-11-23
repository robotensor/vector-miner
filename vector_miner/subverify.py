from __future__ import annotations

import datetime as dt
import hashlib
import re
import statistics

from . import verify as rv
from .protocol import BadImage, parse_image
from .verify import (MAX_SCENES, VerifyError, _arr, _bad, _env, _flag, _int, _num, _obj, _str, _time, base_seed,
                     scenes_digest)

try:
    from vector_env.rules import INFRA_OUTCOMES
except ImportError:
    INFRA_OUTCOMES = ("harness_error", "scene_error", "docker_unavailable", "image_missing", "pull_failed", "start_failed")

FORMAT = "vector-submission-verify/1"
SEED_N = 0
FINAL = ("below_king", "crowned", "not_crowned", "disqualified")
HASH = re.compile(r"^[0-9a-f]{16}$")
INFRA = frozenset(INFRA_OUTCOMES)
NOT_RUN = "not_run"
GENESIS_KING = "king: no image published"


def is_infra(outcome, success) -> bool:
    return not success and (outcome in INFRA or not outcome)


def phase2_sample(salt: str, idxs: list[int], k: int) -> list[int]:
    ranked = sorted(idxs, key=lambda i: hashlib.sha256(f"{salt}:phase2:{int(i)}".encode()).hexdigest())
    return sorted(ranked[:max(0, int(k))])


def _hash(v, where: str) -> None:
    if v is not None and (not isinstance(v, str) or not HASH.match(v)):
        raise _bad(where, "is not an action hash (16 lower-case hex digits) or null")


def _results(res: dict, where: str) -> None:
    for j, v in enumerate(_arr(res.get("success"), f"{where}.success", MAX_SCENES)):
        if v not in (0, 1) or not isinstance(v, (int, bool)):
            raise _bad(f"{where}.success[{j}]", "is not 0 or 1")
    for j, v in enumerate(_arr(res.get("t"), f"{where}.t", MAX_SCENES)):
        _num(v, f"{where}.t[{j}]")
    for j, v in enumerate(_arr(res.get("outcome"), f"{where}.outcome", MAX_SCENES)):
        _str(v, f"{where}.outcome[{j}]", 64)
    for j, v in enumerate(_arr(res.get("action_hash"), f"{where}.action_hash", MAX_SCENES)):
        _hash(v, f"{where}.action_hash[{j}]")


def _ints(v, where: str) -> None:
    for j, x in enumerate(_arr(v, where, MAX_SCENES, null=True)):
        _int(x, f"{where}[{j}]", 0, 2 * MAX_SCENES)


def _who(v: dict, where: str) -> None:
    for f in ("id", "hotkey", "image", "digest", "status"):
        _str(v.get(f), f"{where}.{f}")
    _str(v.get("reason"), f"{where}.reason", 2000)


def validate(doc: dict) -> None:
    sub = _obj(doc.get("submission"), "submission", null=False)
    _who(sub, "submission")
    for k in ("committed_at", "finished_at"):
        _time(sub.get(k), f"submission.{k}")
    _str(doc.get("salt"), "salt", null=False)
    if not doc["salt"]:
        raise _bad("salt", "is empty")
    _str(doc.get("salt_sha256"), "salt_sha256", 64)
    for k in ("salt_committed_at", "phase1_started_at"):
        _time(doc.get(k), k)
    _str(doc.get("scenes_sha256"), "scenes_sha256", 64)
    _int(doc.get("scene_count"), "scene_count", 0, MAX_SCENES)
    _int(doc.get("seed_tries"), "seed_tries", 1, rv.MAX_TRIES, null=True)
    for k, x in enumerate(_arr(doc.get("skipped"), "skipped", MAX_SCENES, null=True)):
        _int(x, f"skipped[{k}]", 0, 2 * MAX_SCENES)
    _obj(doc.get("draw"), "draw")
    _num(_obj(doc.get("task"), "task").get("time_limit_s"), "task.time_limit_s")
    for k in ("wall_s", "margin", "step_deadline_s"):
        _num(doc.get(k), k)
    _env(doc.get("env"), "env")
    for k, x in enumerate(_arr(doc.get("scenes"), "scenes", MAX_SCENES)):
        w = f"scenes[{k}]"
        _obj(x, w, null=False)
        _int(x.get("index"), f"{w}.index", 0, 2 * MAX_SCENES)
        _int(x.get("seed"), f"{w}.seed")
        _str(x.get("state_hash"), f"{w}.state_hash", 128, null=False)
        _env(x.get("env"), f"{w}.env")
    p1 = _obj(doc.get("phase1"), "phase1", null=False)
    _num(p1.get("score"), "phase1.score")
    _num(p1.get("mean_time_s"), "phase1.mean_time_s")
    for f in ("successes", "counted", "infra"):
        _int(p1.get(f), f"phase1.{f}", 0, MAX_SCENES, null=True)
    _results(_obj(p1.get("results"), "phase1.results", null=False), "phase1.results")
    p2 = _obj(doc.get("phase2"), "phase2")
    if p2:
        _str(p2.get("status"), "phase2.status", 64)
        _str(p2.get("reason"), "phase2.reason", 2000)
        for j, v in enumerate(_arr(p2.get("scenes"), "phase2.scenes", MAX_SCENES)):
            _int(v, f"phase2.scenes[{j}]", 0, 2 * MAX_SCENES)
        for j, v in enumerate(_arr(p2.get("action_hash"), "phase2.action_hash", MAX_SCENES)):
            _hash(v, f"phase2.action_hash[{j}]")
        for j, v in enumerate(_arr(p2.get("outcome"), "phase2.outcome", MAX_SCENES)):
            _str(v, f"phase2.outcome[{j}]", 64)
        for f in ("mismatched", "wall_timeouts"):
            _ints(p2.get(f), f"phase2.{f}")
        for j, v in enumerate(_arr(p2.get("wall_s"), "phase2.wall_s", MAX_SCENES)):
            _num(v, f"phase2.wall_s[{j}]")
    k = _obj(doc.get("king"), "king")
    if k:
        _who(k, "king")
        _num(k.get("score"), "king.score")
        _num(k.get("mean_time_s"), "king.mean_time_s")
        _int(k.get("successes"), "king.successes", 0, MAX_SCENES, null=True)
        _results(_obj(k.get("results"), "king.results", null=False), "king.results")
    dec = _obj(doc.get("decision"), "decision")
    for f in ("king_best", "king_score", "score"):
        _num(dec.get(f), f"decision.{f}")
    _int(dec.get("compared"), "decision.compared", 0, MAX_SCENES, null=True)
    _str(dec.get("king"), "decision.king")
    for f in ("phase2_ran", "beats_king", "crowned"):
        _flag(dec.get(f), f"decision.{f}")


def pin_problem(who: dict) -> str | None:
    ref = who.get("image")
    if not ref:
        return "no image published"
    try:
        got = parse_image(ref)["digest"]
    except BadImage as e:
        return f"image {ref!r} is not an accepted pinned image ({e})"
    if who.get("digest") and got != who["digest"]:
        return f"image {ref!r} is not pinned to the digest {who['digest']}"
    return None


def _aware(s: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


UNCOMMITTED = ("the submission has no salt commitment time (salt_committed_at is null): nothing shows the salt was "
               "fixed before phase 1 started")


def _salt_problems(doc: dict, expect: str | None, source: str, allow_uncommitted: bool, warnings: list) -> list[str]:
    bad, have = [], doc.get("salt_sha256")
    if not have:
        bad.append("the document has no salt_sha256: nothing shows the salt was fixed before phase 1")
    elif hashlib.sha256(doc["salt"].encode()).hexdigest() != have:
        bad.append("salt_sha256 is not the sha256 of the salt")
    if expect and have != expect:
        bad.append(f"salt_sha256 {have} is not the commitment {source} ({expect})")
    at, start = doc.get("salt_committed_at"), doc.get("phase1_started_at")
    if not at or not start:
        why = UNCOMMITTED if not at else "the document has no phase1_started_at: the salt commitment time cannot be checked"
        if not allow_uncommitted:
            bad.append(why + "; --allow-uncommitted checks the rest")
        else:
            warnings.append(why + " (--allow-uncommitted)")
    elif _aware(at) > _aware(start):
        bad.append(f"the salt was committed at {at}, after phase 1 started at {start}")
    return bad


def _lengths(name: str, part: dict, count: int) -> list[str]:
    res = part["results"]
    if any(len(res.get(k) or []) != count for k in ("success", "t", "outcome", "action_hash")):
        return [f"{name}: {count} results expected (success, t, outcome, action_hash)"]
    return []


def _times(name: str, part: dict) -> list[str]:
    res = part["results"]
    ts = [float(t) for v, t in zip(res["success"], res["t"]) if v and t is not None]
    if ts and part.get("mean_time_s") is not None and abs(statistics.fmean(ts) - float(part["mean_time_s"])) > 1e-3:
        return [f"{name}: mean time {part['mean_time_s']} is not the mean of its successful scenes"]
    return []


def _near(a, b) -> bool:
    return a is not None and abs(float(a) - float(b)) <= 1e-9


def _verdict(res: dict, k: int) -> bool:
    return not is_infra(res["outcome"][k], res["success"][k])


def _score(name: str, part: dict, count: int) -> list[str]:
    bad = _lengths(name, part, count)
    if bad:
        return bad
    res = part["results"]
    ok = sum(1 for v in res["success"] if v)
    counted = sum(1 for k in range(count) if _verdict(res, k))
    infra = count - counted
    if part.get("successes") is not None and ok != part["successes"]:
        bad.append(f"{name}: {part['successes']} successes published, {ok} in the results")
    if part.get("counted") is not None and part["counted"] != counted:
        bad.append(f"{name}: {part['counted']} scenes counted published, {counted} with a verdict in the results")
    if part.get("infra") is not None and part["infra"] != infra:
        bad.append(f"{name}: {part['infra']} infrastructure failures published, {infra} in the results")
    if not _near(part.get("score"), ok / max(1, counted)):
        left = f" ({infra} infrastructure failures left out)" if infra else ""
        bad.append(f"{name}: score {part.get('score')} is not {ok}/{counted}{left}")
    return bad + _times(name, part)


def same_scenes(doc: dict) -> dict | None:
    p1, king = doc["phase1"]["results"], doc.get("king") or None
    count = len(doc["scenes"])
    if _lengths("phase 1", doc["phase1"], count) or (king is not None and _lengths("king run", king, count)):
        return None
    kr = king["results"] if king else None
    pairs = [k for k in range(count) if _verdict(p1, k) and (kr is None or (
        kr["outcome"][k] != NOT_RUN and _verdict(kr, k)))]
    n = len(pairs)
    c_ok = sum(1 for k in pairs if p1["success"][k])
    k_ok = sum(1 for k in pairs if kr["success"][k]) if kr is not None else 0
    return {"compared": n, "left_out": count - n, "successes": c_ok, "score": c_ok / n if n else 0.0,
            "king_successes": k_ok if kr is not None else None,
            "king_score": (k_ok / n if n else 0.0) if kr is not None else None}


def phase2_check(doc: dict) -> dict | None:
    p2 = doc.get("phase2") or None
    if p2 is None:
        return None
    pos = {x["index"]: k for k, x in enumerate(doc["scenes"])}
    idx, hs, ws = p2.get("scenes") or [], p2.get("action_hash") or [], p2.get("wall_s") or []
    outs = p2.get("outcome") or []
    pub = (doc["phase1"]["results"].get("action_hash") or [])
    cap = doc.get("wall_s")
    mism, over, unknown, infra = [], [], [], []
    for j, i in enumerate(idx):
        if i not in pos or pos[i] >= len(pub):
            unknown.append(i)
            continue
        o = outs[j] if j < len(outs) else None
        if o == NOT_RUN or is_infra(o, False):
            infra.append(i)
            continue
        h, p = (hs[j] if j < len(hs) else None), pub[pos[i]]
        if p is None or h != p:
            mism.append(i)
        w = ws[j] if j < len(ws) else None
        if o == "wall_timeout" or (cap is not None and float(w or 0.0) > float(cap)):
            over.append(i)
    n = len(idx) - len(unknown) - len(infra)
    return {"scenes": len(idx), "compared": n, "equal": n - len(mism), "mismatched": mism, "over_wall": over,
            "infra": infra, "unknown": unknown, "passed": n > 0 and not mism and not over and not unknown,
            "status": p2.get("status"), "reason": p2.get("reason")}


def _sample_problems(doc: dict, p2: dict) -> list[str]:
    res, rows = doc["phase1"]["results"], doc["scenes"]
    if len(res.get("action_hash") or []) != len(rows) or len(res.get("outcome") or []) != len(rows):
        return []
    ok = [x["index"] for k, x in enumerate(rows)
          if res["action_hash"][k] and res["outcome"][k] != NOT_RUN and _verdict(res, k)]
    got = doc["phase2"].get("scenes") or []
    want = phase2_sample(doc["salt"], ok, len(got))
    if got != want:
        return [f"phase 2 ran on other scenes than the sample rule picks from the {len(ok)} phase-1 verdicts "
                f"(the {len(got)} smallest sha256(salt:phase2:index), sorted: {want[:10]})"]
    return []


def _king(king: dict, count: int, dec: dict, warnings: list) -> list[str]:
    bad = _lengths("king run", king, count)
    if bad:
        return bad
    ok = sum(1 for v in king["results"]["success"] if v)
    if king.get("successes") is not None and ok != king["successes"]:
        bad.append(f"king run: {king['successes']} successes published, {ok} in the results")
    bad += _times("king run", king)
    why = pin_problem(king)
    if why and king.get("image") is None and king.get("id") is None:
        warnings.append(f"{GENESIS_KING} (the genesis king runs as a command; its run cannot be repeated here)")
    elif why:
        bad.append(f"king: {why}")
    if dec.get("king") is not None and dec["king"] != king.get("hotkey"):
        bad.append(f"the decision's king is {dec['king']}, the king run is {king.get('hotkey')}")
    return bad


def _decision(doc: dict, st: str, margin: float) -> list[str]:
    same, king, dec = same_scenes(doc), doc.get("king") or None, doc.get("decision") or {}
    if same is None:
        return []
    bad, n, cs, ks = [], same["compared"], same["score"], same["king_score"]
    if king is not None and not _near(king.get("score"), ks):
        bad.append(f"king run: score {king.get('score')} is not {same['king_successes']}/{n}, its successes on the "
                   "scenes where both runs gave a verdict")
    if dec.get("score") is not None and not _near(dec["score"], cs):
        bad.append(f"decision.score {dec['score']} is not {same['successes']}/{n}, the phase-1 successes on the scenes "
                   "where both runs gave a verdict")
    if dec.get("compared") is not None and dec["compared"] != n:
        bad.append(f"decision.compared {dec['compared']} is not {n}, the scenes where both runs gave a verdict")
    if dec.get("king_score") is not None and (ks is None or not _near(dec["king_score"], ks)):
        bad.append(f"decision.king_score {dec['king_score']} is not the king run's {ks}")
    beats = n > 0 and cs > (ks or 0.0) + margin
    if beats != bool(dec.get("beats_king")):
        bad.append(f"beats_king should be {beats}")
    if (st == "crowned") != beats:
        bad.append(f"{st}, but {cs} against the king's {ks or 0.0} + margin {margin} on the same {n} scenes says "
                   f"{'crowned' if beats else 'not_crowned'}")
    return bad


def check_document(doc: dict, salt_sha256: str | None = None, salt_source: str = "you gave",
                   allow_uncommitted: bool = False, warnings: list | None = None) -> list[str]:
    warnings = [] if warnings is None else warnings
    if doc.get("format") != FORMAT:
        return [f"unknown document format {doc.get('format')!r} (this vector-miner reads {FORMAT})"]
    validate(doc)
    bad = []
    sub, salt, rows = doc["submission"], doc["salt"], doc["scenes"]
    st = sub.get("status")
    count, tries = doc["scene_count"], doc.get("seed_tries") or 6
    skip = doc.get("skipped") or []
    if st not in FINAL:
        bad.append(f"the submission is {st}, not final ({', '.join(FINAL)})")
    idx = [x["index"] for x in rows]
    if len(rows) != count or sorted(idx + skip) != list(range(count + len(skip))):
        bad.append(f"the scene list does not hold indexes 0..{count + len(skip) - 1}"
                   + (" less the skipped ones" if skip else ""))
    bad += _salt_problems(doc, salt_sha256, salt_source, allow_uncommitted, warnings)
    if scenes_digest(rows) != doc.get("scenes_sha256"):
        bad.append("scenes_sha256 does not match the scene list")
    for x in rows:
        if not 0 <= int(x["seed"]) - base_seed(salt, SEED_N, x["index"]) < tries:
            bad.append(f"scene {x['index']}: seed {x['seed']} does not derive from the salt")
    why = pin_problem(sub)
    if why:
        bad.append(f"submission: {why}")
    p1 = doc["phase1"]
    bad += _score("phase 1", p1, count)
    margin = float(doc.get("margin") or 0.0)
    dec = doc.get("decision") or {}
    best = dec.get("king_best")
    score = float(p1.get("score") or 0.0)
    p2 = phase2_check(doc)
    king = doc.get("king") or None
    eligible = best is None or score > float(best) + margin
    if bool(dec.get("phase2_ran")) != (p2 is not None):
        bad.append("decision.phase2_ran disagrees with the phase2 section")
    if st == "below_king":
        if eligible:
            bad.append(f"below_king, but the phase-1 score {score} is above the king's best {best} + margin {margin}")
        if p2 is not None or king is not None:
            bad.append("below_king, but phase 2 or a king run is published")
    elif st in FINAL:
        if not eligible:
            bad.append(f"phase 2 ran, but the phase-1 score {score} is not above the king's best {best} + margin {margin}")
        if p2 is None:
            bad.append(f"{st}, but no phase 2 is published")
        else:
            if p2["unknown"]:
                bad.append(f"phase 2 names scenes that are not in the list: {p2['unknown'][:10]}")
            if len(set(doc["phase2"].get("scenes") or [])) != p2["scenes"]:
                bad.append("phase 2 repeats a scene")
            if any(len(doc["phase2"].get(k) or []) != p2["scenes"] for k in ("outcome", "action_hash", "wall_s")):
                bad.append("phase 2 needs one outcome, one action_hash and one wall_s per scene")
            bad += _sample_problems(doc, p2)
            for f, mine in (("mismatched", p2["mismatched"]), ("wall_timeouts", p2["over_wall"])):
                have = doc["phase2"].get(f)
                if have is not None and sorted(have) != sorted(mine):
                    bad.append(f"phase2.{f} {sorted(have)[:10]} is not {sorted(mine)[:10]}, the recomputed list "
                               f"(infrastructure failures left out: {p2['infra'][:10]})")
            if st == "disqualified" and p2["passed"]:
                bad.append("disqualified, but every phase-2 action hash equals phase 1's and every scene fits wall_s")
            if st == "disqualified" and not (doc["phase2"].get("reason") or sub.get("reason")):
                bad.append("disqualified without a reason")
            if st in ("crowned", "not_crowned") and not p2["passed"]:
                bad.append(f"{st}, but phase 2 did not pass (mismatched {p2['mismatched'][:10]}, "
                           f"over the wall {p2['over_wall'][:10]}, compared {p2['compared']})")
        if st == "disqualified" and king is not None:
            bad.append("disqualified, but a king run is published")
        if st in ("crowned", "not_crowned"):
            no_king = king is None and dec.get("king") is None and dec.get("king_score") is None
            if king is None and not no_king:
                bad.append(f"{st}, but no king run on the same scenes is published")
            else:
                if king is not None:
                    bad += _king(king, count, dec, warnings)
                bad += _decision(doc, st, margin)
    if bool(dec.get("crowned")) != (st == "crowned"):
        bad.append("decision.crowned disagrees with the status")
    return bad


def pick(doc: dict, ref: str | None) -> tuple[str, dict]:
    sub, king = doc["submission"], doc.get("king") or None
    if not ref:
        return "submission", sub
    ref = ref.strip()
    for role, who in (("submission", sub), ("king", king)):
        if who is None:
            continue
        keys = {who.get("image"), who.get("digest"), who.get("hotkey"), who.get("id"), role}
        if ref in keys or (who.get("digest") and ref.endswith("@" + who["digest"])):
            return role, who
    have = ", ".join(x.get("image") or "no image published" for x in (sub, king) if x) or "none"
    raise VerifyError(f"{ref} did not run for submission {sub.get('id')} (ran: {have})")


def rerun(doc: dict, role: str, expert: dict, scenes: dict, e, *, wall_s: float | None = None, workers: int = 1,
          docker: dict | None = None, log=rv._log) -> dict:
    part = doc["phase1"] if role == "submission" else doc["king"]
    res = part["results"]
    pos = {x["index"]: k for k, x in enumerate(doc["scenes"])}
    limit = (doc.get("task") or {}).get("time_limit_s")
    if limit is None:
        raise VerifyError("the document has no task.time_limit_s: cannot re-run an image")
    idxs = list(scenes)

    def show(j, r):
        log(f"run {j + 1}/{len(idxs)}: {r.get('scene_id')} {r.get('outcome')} t={r.get('t')} hash={r.get('action_hash')}")

    try:
        got = e.harness.run(expert, [scenes[i] for i in idxs], time_limit_s=float(limit),
                            wall_s=float(wall_s or doc.get("wall_s") or e.wall_s), workers=int(workers), docker=docker,
                            on_result=show)
    except Exception as ex:
        raise VerifyError(f"the re-run did not start here: {type(ex).__name__}: {ex}") from None
    got = [r if isinstance(r, dict) else {} for r in got]
    infra = sum(1 for r in got if r.get("infra"))
    if got and infra == len(got):
        raise VerifyError(f"the re-run did not run here: every scene ended {got[0].get('outcome')} "
                          f"({got[0].get('error')})")
    rows, same_hash, same_out = [], 0, 0
    for i, r in zip(idxs, got):
        k = pos[i]
        pub = {"action_hash": res["action_hash"][k], "outcome": res["outcome"][k], "success": bool(res["success"][k])}
        loc = {"action_hash": r.get("action_hash"), "outcome": r.get("outcome"), "success": bool(r.get("success"))}
        eq = pub["action_hash"] is not None and pub["action_hash"] == loc["action_hash"]
        same_hash += eq
        same_out += pub["outcome"] == loc["outcome"]
        if not eq:
            rows.append({"index": i, "published": pub, "local": loc, "infra": bool(r.get("infra"))})
    k = max(1, len(idxs))
    return {"role": role, "image": (doc["submission"] if role == "submission" else doc["king"]).get("image"),
            "ran": expert.get("image") or "command", "scenes": len(idxs), "hash_equal": same_hash,
            "hash_agreement": round(same_hash / k, 4), "outcome_agreement": round(same_out / k, 4),
            "local_successes": sum(1 for r in got if r.get("success")), "local_infra": infra, "mismatched": rows}


def verify(doc: dict, *, e=None, hash_scenes: int | None = None, image: str | None = None,
           expert: dict | None = None, rerun_scenes: int = 20, sample_seed: int = 0, wall_s: float | None = None,
           workers: int = 1, docker: dict | None = None, salt_sha256: str | None = None,
           salt_source: str = "you gave", pull: bool = False, allow_uncommitted: bool = False, log=rv._log) -> dict:
    warnings: list[str] = []
    problems = check_document(doc, salt_sha256=salt_sha256, salt_source=salt_source,
                              allow_uncommitted=allow_uncommitted, warnings=warnings)
    sub = doc.get("submission") if isinstance(doc.get("submission"), dict) else {}
    out = {"submission": sub.get("id"), "hotkey": sub.get("hotkey"), "status": sub.get("status"),
           "salt": doc.get("salt"), "scene_count": doc.get("scene_count"),
           "document": {"ok": not problems, "problems": problems, "warnings": warnings}}
    for w in warnings:
        log(f"warning: {w}")
    if problems and doc.get("format") != FORMAT:
        out["ok"] = False
        return out
    out["phase2"] = phase2_check(doc)
    out["same_scenes"] = same_scenes(doc) if out["status"] in ("crowned", "not_crowned") else None
    role, who = pick(doc, image) if image or expert else ("submission", None)
    skip_rerun = None
    if who is not None and expert is None:
        skip_rerun = pin_problem(who)
        if skip_rerun:
            log(f"warning: {role}: {skip_rerun}; not re-running it")
        else:
            expert = {"image": who["image"]}
    e = e or rv.load_env()
    local = rv.env_info(e)
    pub = doc.get("env") or {}
    match = rv.same_env(local, pub)
    out["env"] = {"published": pub, "local": local, "match": match}
    if not match:
        log(f"warning: this vector-env ({local.get('version')}, assets {str(local.get('assets_sha256'))[:12]}) is not "
            f"the submission's ({pub.get('version')}, assets {str(pub.get('assets_sha256'))[:12]}); state hashes can differ")
    if pull and expert is not None and expert.get("image"):
        try:
            e.harness.pull(expert["image"], docker)
        except Exception as ex:
            raise VerifyError(f"cannot pull {expert['image']}: {ex}") from None
    idx = sorted(x["index"] for x in doc["scenes"])
    count = len(idx)
    order = [idx[j] for j in rv.sample_order(count, sample_seed)]
    pick_idx = []
    if who is not None:
        res, pos = who.get("results") or doc["phase1"]["results"], {x["index"]: k for k, x in enumerate(doc["scenes"])}
        n = min(len(res.get(k) or []) for k in ("success", "outcome", "action_hash"))
        ran = [i for i in order if pos[i] < n and res["action_hash"][pos[i]] is not None and _verdict(res, pos[i])]
        pick_idx = ran[:max(0, int(rerun_scenes))]
    m = count if hash_scenes is None else min(count, max(0, int(hash_scenes)))
    g = rv.regenerate(doc, e, sorted(set(order[:m]) | set(pick_idx)), log=log, local=local,
                      skipped=sorted(doc.get("skipped") or []) if m == count else [], n=SEED_N)
    made = g.pop("scenes")
    out["scenes"] = g
    if who is not None:
        if skip_rerun:
            out["rerun"] = {"role": role, "image": who.get("image"), "skipped": skip_rerun}
        else:
            todo = {i: made[i] for i in sorted(pick_idx) if i in made}
            out["rerun"] = rerun(doc, role, expert, todo, e, wall_s=wall_s, workers=workers, docker=docker, log=log)
    out["ok"] = not problems and not g["mismatched"] and not g["unverified"]
    return out
