from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
import statistics
from fractions import Fraction

from vector_miner.protocol import BadImage, parse_image

from . import verify as rv
from .verify import (MAX_SCENES, VerifyError, _arr, _bad, _env, _flag, _int, _num, _obj, _str, _time, base_seed,
                     scenes_digest)

try:
    from vector_env.rules import INFRA_OUTCOMES
except ImportError:
    INFRA_OUTCOMES = ("harness_error", "scene_error", "docker_unavailable", "image_missing", "pull_failed", "start_failed", "policy_unavailable", "physics_error")

FORMAT = "vector-submission-verify/1"
SEED_N = 0
FINAL = ("below_king", "ranked", "not_ranked", "disqualified", "crowned", "not_crowned")
LEGACY = ("crowned", "not_crowned")
RANKS = 3
HASH = re.compile(r"^[0-9a-f]{16}$")
INFRA = frozenset(INFRA_OUTCOMES)
NOT_RUN = "not_run"
CUT_OFF = frozenset({"step_timeout", "disconnected", "wall_timeout"})
OUR_SIDE = ("image no longer available", "repeated failures on our side")
FORMULAS = ("pile-1", "pile-2")
SCORE_KEYS = ("time_weight", "path_weight", "path_ref_m", "out_unit")
OUTS_CAP = 30
SCORE_TOL = 1e-4  # per scene: published scores carry 6 decimals and t 3
MEAN_TOL = 1e-5
MAX_PICKS = 1000


def frac(x) -> Fraction:
    return Fraction(x).limit_denominator(10 ** 6)


def margin_frac(m) -> Fraction:
    return Fraction(repr(float(m or 0.0)))


def above(score, best, margin) -> bool:
    return frac(score or 0) > frac(best or 0) + margin_frac(margin)


def ended_on_our_side(doc: dict) -> bool:
    sub = doc.get("submission") or {}
    return sub.get("status") == "disqualified" and str(sub.get("reason") or "").startswith(OUR_SIDE)


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
    for k in ("score", "tcp_path_m", "joint_path_rad"):
        for j, v in enumerate(_arr(res.get(k), f"{where}.{k}", MAX_SCENES, null=True)):
            _num(v, f"{where}.{k}[{j}]")
    for k in ("outs", "crate_hits", "picks_planned"):
        for j, v in enumerate(_arr(res.get(k), f"{where}.{k}", MAX_SCENES, null=True)):
            _int(v, f"{where}.{k}[{j}]", 0, 1 << 31, null=True)
    for j, v in enumerate(_arr(res.get("target_class"), f"{where}.target_class", MAX_SCENES, null=True)):
        _str(v, f"{where}.target_class[{j}]", 32)
    for j, v in enumerate(_arr(res.get("picks"), f"{where}.picks", MAX_SCENES, null=True)):
        for i, pk in enumerate(_arr(v, f"{where}.picks[{j}]", MAX_PICKS, null=True)):
            w = f"{where}.picks[{j}][{i}]"
            _obj(pk, w, null=False)
            for k in ("t", "tcp_path_m", "joint_path_rad", "score"):
                _num(pk.get(k), f"{w}.{k}")
            for k in ("outs", "crate_hits", "tick", "target"):
                _int(pk.get(k), f"{w}.{k}", 0, 1 << 31, null=True)
            for k in ("outcome", "target_class"):
                _str(pk.get(k), f"{w}.{k}", 64)


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
    for k in ("wall_s", "margin", "step_deadline_s", "first_step_deadline_s"):
        _num(doc.get(k), k)
    _int(doc.get("phase2_scenes"), "phase2_scenes", 0, MAX_SCENES, null=True)
    for j, v in enumerate(_arr(doc.get("excluded_outcomes"), "excluded_outcomes", 256, null=True)):
        _str(v, f"excluded_outcomes[{j}]", 64, null=False)
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
    _int(p1.get("outs"), "phase1.outs", 0, 1 << 40, null=True)
    sf = _obj(doc.get("score_formula"), "score_formula")
    _str(sf.get("formula"), "score_formula.formula", 64)
    for k in SCORE_KEYS:
        _num(sf.get(k), f"score_formula.{k}")
    _num(doc.get("final_score"), "final_score")
    _num(doc.get("min_score"), "min_score")
    ev = _obj(doc.get("phase2_eval"), "phase2_eval")
    for k in ("score", "weight", "mean_time_s"):
        _num(ev.get(k), f"phase2_eval.{k}")
    for k in ("scenes", "counted", "successes", "infra"):
        _int(ev.get(k), f"phase2_eval.{k}", 0, MAX_SCENES, null=True)
    _int(ev.get("outs"), "phase2_eval.outs", 0, 1 << 40, null=True)
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
    _str(sub.get("mirror_url"), "submission.mirror_url")
    _int(sub.get("round"), "submission.round", 0, 1 << 31, null=True)
    dec = _obj(doc.get("decision"), "decision")
    _int(dec.get("round"), "decision.round", 0, 1 << 31, null=True)
    _num(dec.get("bar"), "decision.bar")
    _int(dec.get("rank_at"), "decision.rank_at", 1, RANKS, null=True)
    for j, v in enumerate(_arr(dec.get("new"), "decision.new", RANKS, null=True)):
        _str(v, f"decision.new[{j}]", null=False)
    _num(dec.get("king_best"), "decision.king_best")
    for f in ("phase2_ran", "crowned"):
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


def _pick_list(res: dict, count: int) -> list | None:
    p = res.get("picks")
    return p if isinstance(p, list) and len(p) == count else None


def _pick_successes(res: dict, count: int) -> int | None:
    """Successful picks over the scenes of several picks (results.picks), None without picks."""
    p = _pick_list(res, count)
    if p is None:
        return None
    return sum(1 for x in p if isinstance(x, list) for pk in x
               if isinstance(pk, dict) and (pk.get("outcome") == "success" or pk.get("success") is True))


def _times(name: str, part: dict) -> list[str]:
    res = part["results"]
    ts = [float(t) for v, t in zip(res["success"], res["t"]) if v and t is not None]
    p = _pick_list(res, len(res["success"]))
    # several picks per scene: the mean of the successful scenes' whole-scene times, or of the successful picks' times
    tp = [float(pk["t"]) for x in (p or []) if isinstance(x, list) for pk in x if isinstance(pk, dict)
          and pk.get("t") is not None and (pk.get("outcome") == "success" or pk.get("success") is True)]
    have = part.get("mean_time_s")
    if have is None or not (ts or tp):
        return []
    if any(xs and abs(statistics.fmean(xs) - float(have)) <= 1e-3 for xs in (ts, tp)):
        return []
    return [f"{name}: mean time {have} is not the mean of its successful scenes"
            + (" or of its successful picks" if p is not None else "")]


def _near(a, b) -> bool:
    return a is not None and abs(float(a) - float(b)) <= 1e-9


def _verdict(res: dict, k: int) -> bool:
    return not is_infra(res["outcome"][k], res["success"][k])


def _score(name: str, part: dict, count: int, formula: dict | None = None, time_limit_s=None,
           task: dict | None = None) -> list[str]:
    bad = _lengths(name, part, count)
    if bad:
        return bad
    res = part["results"]
    ok = sum(1 for v in res["success"] if v)
    counted = sum(1 for k in range(count) if _verdict(res, k))
    infra = count - counted
    picked = _pick_successes(res, count)
    if part.get("successes") is not None and part["successes"] not in (ok, picked):
        what = f"{ok} in the results" + (f" ({picked} successful picks)" if picked is not None else "")
        bad.append(f"{name}: {part['successes']} successes published, {what}")
    if part.get("counted") is not None and part["counted"] != counted:
        bad.append(f"{name}: {part['counted']} scenes counted published, {counted} with a verdict in the results")
    if part.get("infra") is not None and part["infra"] != infra:
        bad.append(f"{name}: {part['infra']} infrastructure failures published, {infra} in the results")
    left = f" ({infra} infrastructure failures left out)" if infra else ""
    if formula is None:
        if not _near(part.get("score"), ok / max(1, counted)):
            bad.append(f"{name}: score {part.get('score')} is not {ok}/{counted}{left}")
        return bad + _times(name, part)
    bad += _scene_scores(name, part, count, formula, time_limit_s, left, task)
    return bad + _times(name, part)


def scene_score(success: bool, t, outs, tcp_path_m, formula: dict, time_limit_s: float) -> float:
    """One scene's score under the document's score_formula (pile-1):
    s * (1 - time_weight * min(1, t / time_limit_s)) * (1 - path_weight * min(1, tcp_path_m / path_ref_m))
    - out_unit * (2 ** min(outs, 30) - 1), s = 1 on success, else 0."""
    tw, pw = float(formula.get("time_weight") or 0.0), float(formula.get("path_weight") or 0.0)
    ref, unit = float(formula.get("path_ref_m") or 0.0), float(formula.get("out_unit", 1.0) or 0.0)
    n = max(0, int(outs or 0))
    pen = unit * (2 ** min(n, OUTS_CAP) - 1)
    if not success:
        return 0.0 - pen
    f_time = 1.0 - tw * min(1.0, max(0.0, float(t or 0.0)) / max(1e-9, float(time_limit_s)))
    f_path = 1.0 - pw * (min(1.0, max(0.0, float(tcp_path_m or 0.0)) / ref) if ref > 0 else 0.0)
    return f_time * f_path - pen


def picks_score(picks: list, formula: dict, time_limit_s: float, n_picks: int | None = None) -> tuple[float, int]:
    """A scene of several picks in the same bin: (sum over picks of s_i * f_time_i * f_path_i - out_unit *
    (2 ** min(K, 30) - 1)) / P, K = the outs of all its picks, P = the picks a scene has (`n_picks`, else the picks
    listed). Each pick's t is its own robot time, against the per-pick time limit. Returns (score, K)."""
    unit = float(formula.get("out_unit", 1.0) or 0.0)
    gain, k_outs = 0.0, 0
    for pk in picks:
        pk = pk if isinstance(pk, dict) else {}
        k_outs += max(0, int(pk.get("outs") or 0))
        if pk.get("outcome") == "success" or pk.get("success") is True:
            gain += scene_score(True, pk.get("t"), 0, pk.get("tcp_path_m"), formula, time_limit_s)
    n = int(n_picks) if n_picks else max(1, len(picks))
    return (gain - unit * (2 ** min(k_outs, OUTS_CAP) - 1)) / n, k_outs


def _n_picks(doc_task: dict, formula: dict) -> int | None:
    for v in (formula.get("picks"), (doc_task or {}).get("picks")):
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return v
    return None


def _close(a, b, tol: float) -> bool:
    return a is not None and abs(float(a) - float(b)) <= tol * max(1.0, abs(float(b)))


def _scene_scores(name: str, part: dict, count: int, formula: dict, time_limit_s, left: str,
                  task: dict | None = None) -> list[str]:
    """A document with score_formula: the score is the mean scene score over the counted scenes, and each scene's
    score follows from the formula, the task's time limit and the scene's success, t, outs and tcp_path_m, or, for a
    scene of several picks (results.picks), from its picks (picks_score)."""
    res, bad = part["results"], []
    picks = res.get("picks") if isinstance(res.get("picks"), list) and len(res["picks"]) == count else None
    n_picks = _n_picks(task or {}, formula)
    planned = res.get("picks_planned") if isinstance(res.get("picks_planned"), list) and \
        len(res["picks_planned"]) == count else None
    scores = res.get("score")
    if not isinstance(scores, list) or len(scores) != count:
        return [f"{name}: the document has a score_formula, so {count} scene scores are expected (results.score)"]
    if formula.get("formula") not in FORMULAS:
        return [f"{name}: unknown score formula {formula.get('formula')!r} (this vector-miner knows "
                f"{', '.join(FORMULAS)}); the scene scores are not checked"]
    if time_limit_s is None:
        return [f"{name}: the document has no task.time_limit_s: the scene scores cannot be checked"]
    outs = res.get("outs") if isinstance(res.get("outs"), list) and len(res["outs"]) == count else None
    path = res.get("tcp_path_m") if isinstance(res.get("tcp_path_m"), list) and len(res["tcp_path_m"]) == count else None
    got, wrong, total_outs = [], [], 0
    for k in range(count):
        if not _verdict(res, k):
            continue
        o = outs[k] if outs else 0
        if picks is not None and isinstance(picks[k], list) and picks[k]:
            want, k_outs = picks_score(picks[k], formula, time_limit_s, (planned[k] if planned else None) or n_picks)
            if outs and int(o or 0) != k_outs:
                bad.append(f"{name}: scene {k + 1} of the list has {o} outs, its picks {k_outs}")
            o = k_outs
        else:
            want = scene_score(bool(res["success"][k]), res["t"][k], o, path[k] if path else 0.0, formula,
                               time_limit_s)
            if picks is not None and n_picks:
                want /= n_picks
        total_outs += int(o or 0)
        have = scores[k]
        if have is None:
            have = 0.0 if res["outcome"][k] == NOT_RUN else None
        if have is None or not _close(have, want, SCORE_TOL):
            wrong.append((k, have, want))
        got.append(float(have) if have is not None else want)
    if wrong:
        k, have, want = wrong[0]
        bad.append(f"{name}: {len(wrong)} scene scores do not follow the score formula (first: scene "
                   f"{k + 1} of the list published {have}, the formula gives {round(want, 6)})")
    mean = math.fsum(got) / len(got) if got else 0.0
    if part.get("score") is None or not _close(part["score"], mean, MEAN_TOL):
        bad.append(f"{name}: score {part.get('score')} is not the mean scene score {round(mean, 6)} of the "
                   f"{len(got)} counted scenes{left}")
    if outs and part.get("outs") is not None and int(part["outs"]) != total_outs:
        bad.append(f"{name}: {part['outs']} outs published, {total_outs} in the counted scenes' results")
    return bad


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


def excluded(doc: dict) -> frozenset:
    got = doc.get("excluded_outcomes")
    return frozenset(got) | {NOT_RUN} if isinstance(got, list) else frozenset({NOT_RUN})


def _excluded_problems(doc: dict) -> list[str]:
    got = doc.get("excluded_outcomes")
    if not isinstance(got, list):
        return []
    extra = sorted(set(got) - INFRA - CUT_OFF - {NOT_RUN})
    if extra:
        return [f"excluded_outcomes leaves {', '.join(extra)} out of the phase-2 sample; the rule leaves out only "
                f"infrastructure failures, {NOT_RUN} and the cut-offs ({', '.join(sorted(CUT_OFF))})"]
    return []


def _sample_problems(doc: dict, p2: dict) -> list[str]:
    res, rows = doc["phase1"]["results"], doc["scenes"]
    if len(res.get("action_hash") or []) != len(rows) or len(res.get("outcome") or []) != len(rows):
        return []
    out = excluded(doc)
    ok = [x["index"] for k, x in enumerate(rows)
          if res["action_hash"][k] and res["outcome"][k] not in out and _verdict(res, k)]
    got = doc["phase2"].get("scenes") or []
    k = doc.get("phase2_scenes")
    want = phase2_sample(doc["salt"], ok, len(got) if k is None else k)
    if got != want:
        size = f"{len(want)} = min(phase2_scenes {k}, {len(ok)})" if k is not None else str(len(got))
        left = f", leaving out {', '.join(sorted(out))}" if doc.get("excluded_outcomes") is not None else ""
        return [f"phase 2 ran on other scenes than the sample rule picks from the {len(ok)} eligible phase-1 "
                f"scenes{left} (the {size} smallest sha256(salt:phase2:index), sorted: {want[:10]})"]
    return []


def is_legacy(doc: dict) -> bool:
    dec = doc.get("decision") if isinstance(doc.get("decision"), dict) else {}
    sub = doc.get("submission") if isinstance(doc.get("submission"), dict) else {}
    old = "king" in doc or "decisive" in doc or "king_best" in dec
    return sub.get("status") in LEGACY or (old and "bar" not in dec and "rank_at" not in dec)


def final_of(doc: dict):
    """The score the round decision ranks by: final_score, else (older documents, no phase 2) the phase-1 score."""
    got = doc.get("final_score")
    return got if got is not None else (doc.get("phase1") or {}).get("score")


def eligible(doc: dict, final: bool = False) -> bool:
    """The phase-1 score (final: the final score) is above the minimum score (min_score, 0 when the document has
    none) and above the bar (decision.bar, the king's score + margin; null: no king). The phase-1 score is compared
    with the bar before phase 2, the final score at the round decision. An older document has decision.king_best
    and the margin instead."""
    dec = doc.get("decision") or {}
    score = (final_of(doc) if final else (doc.get("phase1") or {}).get("score")) or 0
    if is_legacy(doc):
        best = dec.get("king_best")
        return best is None or above(score, best, doc.get("margin"))
    if frac(score) <= frac(min_score(doc)):
        return False
    return dec.get("bar") is None or frac(score) > frac(dec["bar"])


def min_score(doc: dict) -> float:
    """The floor a score must be above to rank: the document's min_score, else 0."""
    v = doc.get("min_score")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else 0.0


def decision_check(doc: dict) -> dict | None:
    dec = doc.get("decision") if isinstance(doc.get("decision"), dict) else None
    if dec is None or is_legacy(doc):
        return None
    sid, new = (doc.get("submission") or {}).get("id"), dec.get("new") or []
    return {"round": dec.get("round"), "bar": dec.get("bar"), "rank_at": dec.get("rank_at"), "new": len(new),
            "entered": sid in new, "above_bar": eligible(doc, final=True)}


def _decision_problems(doc: dict, st: str, p2: dict | None) -> list[str]:
    sub, dec, bad = doc["submission"], doc.get("decision") or {}, []
    if not dec:
        return [f"{st}, but the document has no decision"] if st in ("ranked", "not_ranked") else []
    new, at, sid = dec.get("new") or [], dec.get("rank_at"), sub.get("id")
    if dec.get("round") is not None and sub.get("round") is not None and dec["round"] != sub["round"]:
        bad.append(f"decision.round {dec['round']} is not the submission's round {sub['round']}")
    if len(set(new)) != len(new):
        bad.append("decision.new repeats a submission")
    if st == "ranked":
        if sid not in new:
            bad.append(f"ranked, but decision.new does not name {sid}")
        if at is None:
            bad.append("ranked, but decision.rank_at is null")
        elif at > len(new):
            bad.append(f"rank_at {at}, but only {len(new)} submissions entered (the new ones take ranks 1..{len(new)})")
        if not eligible(doc, final=True):
            bad.append(f"ranked, but the final score {final_of(doc)} is not above the bar {dec.get('bar')}")
    else:
        if at is not None:
            bad.append(f"{st}, but decision.rank_at is {at}")
        if sid in new:
            bad.append(f"{st}, but decision.new names this submission")
    if st == "not_ranked" and eligible(doc, final=True) and p2 is not None and p2["passed"] and len(new) < RANKS:
        bad.append(f"not_ranked, but its score is above the bar {dec.get('bar')} and only {len(new)} submissions "
                   f"entered at round {dec.get('round')}'s decision (up to {RANKS} do)")
    return bad


def _final_problems(doc: dict, st: str, warnings: list) -> list[str]:
    """final_score = the private evaluation's score when the round had one (phase2_eval, weight 1; a document with
    another weight w: (1 - w) * phase-1 score + w * that score), else the phase-1 score. The private scenes are not
    published: its score is taken as published."""
    fs, ev = doc.get("final_score"), doc.get("phase2_eval") or None
    p1 = (doc.get("phase1") or {}).get("score")
    if fs is None:
        if st in ("ranked", "not_ranked") and ev is not None:
            return [f"{st} after a private evaluation, but the document has no final_score"]
        return []
    if p1 is None:
        return ["final_score is published without a phase-1 score"]
    if ev is None:
        return [] if _close(fs, p1, MEAN_TOL) else [f"final_score {fs} is not the phase-1 score {p1} (no private "
                                                     "evaluation in this round)"]
    w, es = ev.get("weight"), ev.get("score")
    w = 1.0 if w is None else float(w)
    if es is None:
        return [f"final_score {fs} is published, but the private evaluation has no score"]
    want = (1.0 - float(w)) * float(p1) + float(w) * float(es)
    warnings.append(f"the private evaluation ({ev.get('scenes')} scenes, score {es}) is not "
                    "published scene by scene; its score is taken as published")
    if not _close(fs, want, MEAN_TOL):
        if w == 1.0:
            return [f"final_score {fs} is not the private score {es}"]
        return [f"final_score {fs} is not (1 - {w}) * {p1} + {w} * {es} = {round(want, 6)}"]
    return []


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
    bad += _excluded_problems(doc)
    if scenes_digest(rows) != doc.get("scenes_sha256"):
        bad.append("scenes_sha256 does not match the scene list")
    for x in rows:
        if not 0 <= int(x["seed"]) - base_seed(salt, SEED_N, x["index"]) < tries:
            bad.append(f"scene {x['index']}: seed {x['seed']} does not derive from the salt")
    why = pin_problem(sub)
    if why:
        bad.append(f"submission: {why}")
    p1 = doc["phase1"]
    ours = ended_on_our_side(doc)
    if not (ours and p1.get("score") is None):
        task = doc.get("task") or {}
        bad += _score("phase 1", p1, count, doc.get("score_formula") or None, task.get("time_limit_s"), task)
    margin = float(doc.get("margin") or 0.0)
    dec = doc.get("decision") or {}
    legacy = is_legacy(doc)
    score = float(p1.get("score") or 0.0)
    bar = dec.get("king_best") if legacy else dec.get("bar")
    above_bar = eligible(doc)
    what = (f"the king's best {bar} + margin {margin}" if legacy else f"the bar {bar}")
    p2 = phase2_check(doc)
    if legacy:
        warnings.append("an older document: its king run and decisive re-check (if any) are not checked "
                        "(phase 1, phase 2 and the bar are checked)")
    if dec.get("phase2_ran") is not None and bool(dec["phase2_ran"]) != (p2 is not None):
        bad.append("decision.phase2_ran disagrees with the phase2 section")
    if st == "below_king":
        if above_bar:
            bad.append(f"below_king, but the phase-1 score {score} is above {what}"
                       + ("" if bar is not None else f" (no king) and above the minimum score {min_score(doc):g}"))
        if p2 is not None:
            bad.append("below_king, but phase 2 is published")
    elif ours and p2 is None:
        pass
    elif st == "not_ranked" and p2 is None and above_bar and len(dec.get("new") or []) >= RANKS:
        pass
    elif st in FINAL:
        if not above_bar:
            bad.append(f"phase 2 ran, but the phase-1 score {score} is not above {what}")
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
            at_decisive = legacy and doc.get("decisive") is not None
            if st == "disqualified" and p2["passed"] and not ours and not at_decisive:
                bad.append("disqualified, but every phase-2 action hash equals phase 1's and every scene fits wall_s")
            if st == "disqualified" and not (doc["phase2"].get("reason") or sub.get("reason")):
                bad.append("disqualified without a reason")
            if st in ("ranked", "not_ranked", "crowned", "not_crowned") and not p2["passed"]:
                bad.append(f"{st}, but phase 2 did not pass (mismatched {p2['mismatched'][:10]}, "
                           f"over the wall {p2['over_wall'][:10]}, compared {p2['compared']})")
    bad += _final_problems(doc, st, warnings)
    if not legacy:
        bad += _decision_problems(doc, st, p2)
    if dec.get("crowned") is not None and bool(dec["crowned"]) != (st == "crowned"):
        bad.append("decision.crowned disagrees with the status")
    return bad


def pick(doc: dict, ref: str | None, king_image: str | None = None) -> tuple[str, dict]:
    """Which image to re-run: 'submission' (or its reference, digest or hotkey) is the image the submission
    committed; 'king' is the king's public image (king.public_image), for the king's own submission."""
    sub = doc["submission"]
    if not ref:
        return "submission", sub
    ref = ref.strip()
    if ref == "king":
        if not king_image:
            raise VerifyError("--image king needs the king's public image (king.public_image from --api)")
        try:
            digest = parse_image(king_image)["digest"]
        except BadImage as e:
            raise VerifyError(f"the king's public image {king_image!r} is not a pinned image ({e})") from None
        return "king", {"image": king_image, "digest": digest}
    keys = {sub.get("image"), sub.get("digest"), sub.get("hotkey"), sub.get("id"), "submission"}
    if ref in keys or (sub.get("digest") and ref.endswith("@" + sub["digest"])):
        return "submission", sub
    raise VerifyError(f"{ref} is not submission {sub.get('id')}'s image ({sub.get('image') or 'no image published'}); "
                      "--image takes 'submission' (its committed image) or 'king' (the king's public image, "
                      "for the king's submission)")


def rerun(doc: dict, role: str, expert: dict, scenes: dict, e, *, wall_s: float | None = None, workers: int = 1,
          docker: dict | None = None, log=rv._log) -> dict:
    res = doc["phase1"]["results"]
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
    return {"role": role, "image": doc["submission"].get("image"),
            "ran": expert.get("image") or "command", "scenes": len(idxs), "hash_equal": same_hash,
            "hash_agreement": round(same_hash / k, 4), "outcome_agreement": round(same_out / k, 4),
            "local_successes": sum(1 for r in got if r.get("success")), "local_infra": infra, "mismatched": rows}


def verify(doc: dict, *, e=None, hash_scenes: int | None = None, image: str | None = None,
           king_image: str | None = None, expert: dict | None = None, rerun_scenes: int = 20, sample_seed: int = 0, wall_s: float | None = None,
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
    out["decision"] = decision_check(doc)
    role, who = pick(doc, image, king_image) if image or expert else ("submission", None)
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
        res, pos = doc["phase1"]["results"], {x["index"]: k for k, x in enumerate(doc["scenes"])}
        n = min(len(res.get(k) or []) for k in ("success", "outcome", "action_hash"))
        ran = [i for i in order if pos[i] < n and res["action_hash"][pos[i]] is not None and _verdict(res, pos[i])
               and res["outcome"][pos[i]] not in CUT_OFF]
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
