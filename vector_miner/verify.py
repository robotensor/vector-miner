from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import random
import re
import statistics
import sys
from types import SimpleNamespace

from .protocol import BadImage, parse_image

FORMAT = "vector-round-verify/1"
MAX_DOC_BYTES = 64 << 20
MAX_SCENES = 100_000
MAX_IMAGES = 10_000
MAX_TRIES = 64
MAX_STR = 256
ENV_KEYS = ("version", "assets_sha256")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
NOT_RUN = "not_run"
TEAM_EXPERT = "team expert (not publicly re-runnable)"


class VerifyError(Exception):
    pass


def load_env() -> SimpleNamespace:
    try:
        from vector_env import harness, scenes
        from vector_env.settings import env_config
    except ImportError:
        raise VerifyError("regenerating scenes needs vector-env (scripts/setup.sh installs it)") from None
    try:
        wall_s = float(env_config()["harness"]["wall_s"])
    except Exception as e:
        raise VerifyError(f"vector-env's settings do not load: {e}") from None
    return SimpleNamespace(scenes=scenes, harness=harness, wall_s=wall_s)


def _no_constant(name: str):
    raise ValueError(f"{name} is not a JSON number")


def loads(raw: bytes | str) -> dict:
    if len(raw) > MAX_DOC_BYTES:
        raise VerifyError(f"the document is larger than {MAX_DOC_BYTES} bytes")
    try:
        doc = json.loads(raw, parse_constant=_no_constant)
    except (ValueError, RecursionError) as e:
        raise VerifyError(f"the document is not JSON: {e}") from None
    if not isinstance(doc, dict):
        raise VerifyError("the document is not a verify document (not a JSON object)")
    return doc


def read_file(path: str) -> dict:
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_DOC_BYTES + 1)
    except OSError as e:
        raise VerifyError(f"cannot read {path}: {e}") from None
    try:
        return loads(raw)
    except VerifyError as e:
        raise VerifyError(f"cannot read {path}: {e}") from None


def _bad(where: str, why: str) -> VerifyError:
    return VerifyError(f"malformed verify document: {where} {why}")


def _int(v, where: str, lo: int = 0, hi: int = 1 << 63, null: bool = False) -> None:
    if v is None and null:
        return
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise _bad(where, f"is not an integer in {lo}..{hi}")


def _num(v, where: str) -> None:
    if v is None:
        return
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
        raise _bad(where, "is not a finite number")


def _str(v, where: str, n: int = MAX_STR, null: bool = True) -> None:
    if v is None and null:
        return
    if not isinstance(v, str) or len(v) > n:
        raise _bad(where, f"is not a string of at most {n} characters")


def _flag(v, where: str) -> None:
    if v is not None and not isinstance(v, bool):
        raise _bad(where, "is not true or false")


def _obj(v, where: str, null: bool = True) -> dict:
    if v is None and null:
        return {}
    if not isinstance(v, dict):
        raise _bad(where, "is not an object")
    return v


def _arr(v, where: str, n: int, null: bool = False) -> list:
    if v is None and null:
        return []
    if not isinstance(v, list) or len(v) > n:
        raise _bad(where, f"is not a list of at most {n} items")
    return v


def _time(v, where: str) -> None:
    _str(v, where, 64)
    if v is not None:
        try:
            dt.datetime.fromisoformat(v)
        except ValueError:
            raise _bad(where, "is not an ISO time") from None


def _env(v, where: str) -> None:
    for k, x in _obj(v, where).items():
        _str(x, f"{where}.{k}")


def validate(doc: dict) -> None:
    _int(doc.get("round"), "round", 0, 10**9)
    _str(doc.get("salt"), "salt", null=False)
    if not doc["salt"]:
        raise _bad("salt", "is empty")
    _str(doc.get("salt_sha256"), "salt_sha256", 64)
    for k in ("salt_committed_at", "closed_at"):
        _time(doc.get(k), k)
    _str(doc.get("status"), "status")
    _str(doc.get("scenes_sha256"), "scenes_sha256", 64)
    _int(doc.get("scene_count"), "scene_count", 0, MAX_SCENES)
    _int(doc.get("seed_tries"), "seed_tries", 1, MAX_TRIES, null=True)
    for k, x in enumerate(_arr(doc.get("skipped"), "skipped", MAX_SCENES, null=True)):
        _int(x, f"skipped[{k}]", 0, 2 * MAX_SCENES)
    _obj(doc.get("draw"), "draw")
    _num(_obj(doc.get("task"), "task").get("time_limit_s"), "task.time_limit_s")
    _num(doc.get("wall_s"), "wall_s")
    _num(doc.get("margin"), "margin")
    _env(doc.get("env"), "env")
    for k, x in enumerate(_arr(doc.get("scenes"), "scenes", MAX_SCENES)):
        w = f"scenes[{k}]"
        _obj(x, w, null=False)
        _int(x.get("index"), f"{w}.index", 0, 2 * MAX_SCENES)
        _int(x.get("seed"), f"{w}.seed")
        _str(x.get("state_hash"), f"{w}.state_hash", 128, null=False)
        _env(x.get("env"), f"{w}.env")
    dec = _obj(doc.get("decision"), "decision")
    for k in ("king", "winner"):
        _str(dec.get(k), f"decision.{k}")
    for k in ("king_score", "winner_score"):
        _num(dec.get(k), f"decision.{k}")
    for k in ("beats_king", "crowned"):
        _flag(dec.get(k), f"decision.{k}")
    for k, im in enumerate(_arr(doc.get("images"), "images", MAX_IMAGES, null=True)):
        w = f"images[{k}]"
        _obj(im, w, null=False)
        for f in ("role", "hotkey", "image", "run_image", "digest", "status"):
            _str(im.get(f), f"{w}.{f}")
        _str(im.get("stop_reason"), f"{w}.stop_reason", 2000)
        _int(im.get("infra_failures"), f"{w}.infra_failures", 0, MAX_SCENES, null=True)
        _int(im.get("rank"), f"{w}.rank", 1, MAX_IMAGES, null=True)
        _int(im.get("commit_block"), f"{w}.commit_block", null=True)
        _int(im.get("successes"), f"{w}.successes", 0, MAX_SCENES, null=True)
        for f in ("score", "mean_time_s"):
            _num(im.get(f), f"{w}.{f}")
        for f in ("winner", "crowned", "team"):
            _flag(im.get(f), f"{w}.{f}")
        res = _obj(im.get("results"), f"{w}.results", null=False)
        for j, v in enumerate(_arr(res.get("success"), f"{w}.results.success", MAX_SCENES)):
            if v not in (0, 1) or not isinstance(v, (int, bool)):
                raise _bad(f"{w}.results.success[{j}]", "is not 0 or 1")
        for j, v in enumerate(_arr(res.get("t"), f"{w}.results.t", MAX_SCENES)):
            _num(v, f"{w}.results.t[{j}]")
        for j, v in enumerate(_arr(res.get("outcome"), f"{w}.results.outcome", MAX_SCENES)):
            _str(v, f"{w}.results.outcome[{j}]", 64)


def pin_problem(im: dict) -> str | None:
    ref = im.get("run_image")
    if not ref:
        return None
    try:
        got = parse_image(ref)["digest"]
    except BadImage as e:
        return f"run_image {ref!r} is not an accepted pinned image ({e})"
    if got != im.get("digest"):
        return f"run_image {ref!r} is not pinned to the image's digest {im.get('digest')}"
    return None


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
    if im.get("team") and not im.get("run_image"):
        return f"{im.get('role')} {im.get('hotkey')}: {TEAM_EXPERT}"
    return im.get("run_image") or im.get("image") or f"{im.get('role')} {im.get('hotkey')}"


def not_rerunnable(doc: dict) -> list[dict]:
    return [{"role": im.get("role"), "hotkey": im.get("hotkey"),
             "note": TEAM_EXPERT if im.get("team") else "ran a program that is not published"}
            for im in doc.get("images") or [] if not im.get("run_image")]


def _salt_problems(doc: dict, expect: str | None, source: str) -> list[str]:
    bad, have = [], doc.get("salt_sha256")
    if not have:
        bad.append("the document has no salt_sha256: nothing shows the salt was fixed before the round closed")
    elif hashlib.sha256(doc["salt"].encode()).hexdigest() != have:
        bad.append("salt_sha256 is not the sha256 of the salt")
    if expect and have != expect:
        bad.append(f"salt_sha256 {have} is not the commitment {source} ({expect})")
    at, closed = doc.get("salt_committed_at"), doc.get("closed_at")
    if at and closed:
        a, c = dt.datetime.fromisoformat(at), dt.datetime.fromisoformat(closed)
        a, c = (x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc) for x in (a, c))
        if a > c:
            bad.append(f"the salt was committed at {at}, after the round closed at {closed}")
    return bad


def check_document(doc: dict, salt_sha256: str | None = None, salt_source: str = "you gave") -> list[str]:
    if doc.get("format") != FORMAT:
        return [f"unknown document format {doc.get('format')!r} (this vector-miner reads {FORMAT})"]
    validate(doc)
    bad = []
    n, salt, rows = doc["round"], doc["salt"], doc["scenes"]
    count, tries = doc["scene_count"], doc.get("seed_tries") or 6
    skip = doc.get("skipped") or []
    if doc.get("status") != "final":
        bad.append(f"the round is {doc.get('status')}, not final")
    idx = [x["index"] for x in rows]
    if len(rows) != count or sorted(idx + skip) != list(range(count + len(skip))):
        bad.append(f"the scene list does not hold indexes 0..{count + len(skip) - 1}"
                   + (" less the skipped ones" if skip else ""))
    bad += _salt_problems(doc, salt_sha256, salt_source)
    if scenes_digest(rows) != doc.get("scenes_sha256"):
        bad.append("scenes_sha256 does not match the scene list")
    for x in rows:
        if not 0 <= int(x["seed"]) - base_seed(salt, n, x["index"]) < tries:
            bad.append(f"scene {x['index']}: seed {x['seed']} does not derive from the salt")
    images = doc.get("images") or []
    for im in images:
        res, name = im["results"], label(im)
        why = pin_problem(im)
        if why:
            bad.append(f"{name}: {why}")
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
    kings = [x for x in images if x.get("role") == "king"]
    king = kings[0] if kings else None
    if len(kings) > 1:
        bad.append(f"{len(kings)} king images in one round")
    if dec.get("king") is not None and king is None:
        bad.append(f"the decision names the king {dec['king']}, but no king image was evaluated")
    elif king is not None and king.get("hotkey") != dec.get("king"):
        bad.append(f"the king image is {king.get('hotkey')}, the decision's king is {dec.get('king')}")
    if king is None and dec.get("king_score") is not None:
        bad.append("the decision has a king_score without a king image")
    if king is not None and king.get("score") != dec.get("king_score"):
        bad.append("the king's score is not the decision's king_score")
    best = next((x for x in ch if x.get("status") != "infra_failed"), None)
    if best is not None and not best.get("winner"):
        bad.append("the top-ranked challenger is not marked as the winner")
    if any(x.get("winner") for x in images if x is not best):
        bad.append("an image other than the top-ranked challenger is marked as the winner")
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


def env_info(e: SimpleNamespace) -> dict:
    try:
        got = e.scenes.env_info()
    except Exception as ex:
        raise VerifyError(f"vector-env cannot describe its asset bundle: {ex}") from None
    return got if isinstance(got, dict) else {}


def same_env(local: dict, want: dict) -> bool:
    return all(local.get(k) == want.get(k) for k in ENV_KEYS if want.get(k) is not None)


def _draw(e: SimpleNamespace, seed0: int, tries: int, draw) -> tuple[dict | None, str | None]:
    why = None
    for k in range(tries):
        try:
            return e.scenes.generate(seed0 + k, draw), None
        except Exception as ex:
            why = f"{type(ex).__name__}: {ex}"[:500]
    return None, why


def _generator_works(e: SimpleNamespace, rows: dict, failed: set, draw) -> tuple[bool, str | None]:
    probe = next((x for i, x in sorted(rows.items()) if i not in failed), None)
    if probe is None:
        return False, "no other published scene to test the generator on"
    try:
        e.scenes.generate(int(probe["seed"]), draw)
        return True, None
    except Exception as ex:
        return False, f"{type(ex).__name__}: {ex}"[:500]


def regenerate(doc: dict, e: SimpleNamespace, indexes: list[int], log=_log, local: dict | None = None,
               skipped: list[int] = ()) -> dict:
    n, salt, draw = doc["round"], doc["salt"], doc.get("draw")
    tries = doc.get("seed_tries") or 6
    rows = {x["index"]: x for x in doc["scenes"]}
    top = doc.get("env") or {}
    local = env_info(e) if local is None else local
    made, bad, other, failed, works = {}, [], [], set(), False

    def check_works(why):
        nonlocal works
        if works or made:
            works = True
            return
        works, err = _generator_works(e, rows, failed, draw)
        if not works:
            raise VerifyError(f"the scene generator does not work here ({why}; a published scene fails too: {err}): "
                              "check vector-env, node and the asset bundle")

    for j, i in enumerate(indexes):
        x = rows[i]
        want = x.get("env") or top
        sc, why = _draw(e, base_seed(salt, n, i), tries, draw)
        row = {"index": i, "seed": x["seed"], "state_hash": x["state_hash"]}
        if sc is None:
            failed.add(i)
            check_works(why)
            row["error"] = why
        elif not isinstance(sc, dict) or sc.get("seed") != x["seed"] or sc.get("state_hash") != x["state_hash"]:
            row.update(got_seed=sc.get("seed") if isinstance(sc, dict) else None,
                       got_state_hash=sc.get("state_hash") if isinstance(sc, dict) else None)
        else:
            made[i] = sc
            row = None
        tag = "ok"
        if row is not None and same_env(local, want):
            bad.append(row)
            tag = "MISMATCH"
        elif row is not None:
            other.append({**row, "env": want})
            tag = "differs (made with another vector-env)"
        log(f"scene {j + 1}/{len(indexes)}: index {i} {tag}")
    for i in skipped:
        sc, _ = _draw(e, base_seed(salt, n, i), tries, draw)
        if sc is None:
            continue
        row = {"index": i, "skipped": True, "got_seed": sc.get("seed"), "got_state_hash": sc.get("state_hash")}
        (bad if same_env(local, top) else other).append(row)
        log(f"skipped scene {i}: composes a valid scene here")
    return {"checked": len(indexes), "matched": len(made), "mismatched": bad, "unverified": other,
            "skipped_checked": len(skipped), "scenes": made}


def unfinished(doc: dict) -> list[dict]:
    out = []
    for im in doc.get("images") or []:
        nr = sum(1 for o in im["results"]["outcome"] if o == NOT_RUN)
        if im.get("status") in ("stopped", "infra_failed") or nr or im.get("stop_reason"):
            out.append({"image": label(im), "role": im.get("role"), "status": im.get("status"),
                        "stop_reason": im.get("stop_reason"), "not_run": nr,
                        "infra_failures": im.get("infra_failures")})
    return out


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
    limit = (doc.get("task") or {}).get("time_limit_s")
    if limit is None:
        raise VerifyError("the document has no task.time_limit_s: cannot re-run an image")

    def show(j, r):
        log(f"run {j + 1}/{len(idxs)}: {r.get('scene_id')} {r.get('outcome')} t={r.get('t')}")

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
    rows, same_ok, same_out, dts = [], 0, 0, []
    for i, r in zip(idxs, got):
        k = pos[i]
        pub = {"success": bool(res["success"][k]), "t": res["t"][k], "outcome": res["outcome"][k]}
        loc = {"success": bool(r.get("success")), "t": r.get("t"), "outcome": r.get("outcome")}
        same_ok += pub["success"] == loc["success"]
        same_out += pub["outcome"] == loc["outcome"]
        if pub["success"] and loc["success"] and pub["t"] is not None and isinstance(loc["t"], (int, float)):
            dts.append(abs(float(pub["t"]) - float(loc["t"])))
        if pub["success"] != loc["success"] or pub["outcome"] != loc["outcome"]:
            rows.append({"index": i, "published": pub, "local": loc})
    k = max(1, len(idxs))
    return {"image": label(im), "digest": im.get("digest"), "ran": expert.get("image") or "command",
            "scenes": len(idxs), "agreement": round(same_ok / k, 4), "outcome_agreement": round(same_out / k, 4),
            "published_successes": sum(1 for i in idxs if res["success"][pos[i]]),
            "local_successes": sum(1 for r in got if r.get("success")), "local_infra": infra,
            "mean_abs_dt_s": round(statistics.fmean(dts), 3) if dts else None, "disagreements": rows}


def verify(doc: dict, *, e: SimpleNamespace | None = None, hash_scenes: int | None = None, image: str | None = None,
           expert: dict | None = None, rerun_scenes: int = 20, sample_seed: int = 0, wall_s: float | None = None,
           workers: int = 1, docker: dict | None = None, salt_sha256: str | None = None,
           salt_source: str = "you gave", pull: bool = False, log=_log) -> dict:
    problems = check_document(doc, salt_sha256=salt_sha256, salt_source=salt_source)
    out = {"round": doc.get("round"), "salt": doc.get("salt"), "scene_count": doc.get("scene_count"),
           "document": {"ok": not problems, "problems": problems}}
    if problems and doc.get("format") != FORMAT:
        out["ok"] = False
        return out
    out["not_rerunnable"] = not_rerunnable(doc)
    out["unfinished"] = unfinished(doc)
    for x in out["unfinished"]:
        log(f"note: {x['role']} {x['image']} is {x['status']}: {x['not_run']} scenes not run"
            + (f" ({x['stop_reason']})" if x["stop_reason"] else ""))
    im = find_image(doc, image) if image else None
    skip_rerun = None
    if im is not None and expert is None:
        if not im.get("run_image"):
            what = "is the " + TEAM_EXPERT if im.get("team") else "ran a program that is not published"
            raise VerifyError(f"{im.get('role')} {im.get('hotkey')} {what}; give --command to run your own")
        skip_rerun = pin_problem(im)
        if skip_rerun:
            log(f"warning: not running {label(im)}: {skip_rerun}")
        else:
            expert = {"image": im["run_image"]}
    e = e or load_env()
    local = env_info(e)
    pub = doc.get("env") or {}
    match = same_env(local, pub)
    out["env"] = {"published": pub, "local": local, "match": match}
    if not match:
        log(f"warning: this vector-env ({local.get('version')}, assets {str(local.get('assets_sha256'))[:12]}) is not "
            f"the round's ({pub.get('version')}, assets {str(pub.get('assets_sha256'))[:12]}); state hashes can differ")
    if pull and expert is not None and expert.get("image"):
        try:
            e.harness.pull(expert["image"], docker)
        except Exception as ex:
            raise VerifyError(f"cannot pull {expert['image']}: {ex}") from None
    idx = sorted(x["index"] for x in doc["scenes"])
    count = len(idx)
    order = [idx[j] for j in sample_order(count, sample_seed)]
    pick = []
    if im is not None:
        pos = {x["index"]: j for j, x in enumerate(doc["scenes"])}
        pick = [i for i in order if im["results"]["outcome"][pos[i]] != NOT_RUN][:max(0, int(rerun_scenes))]
    m = count if hash_scenes is None else min(count, max(0, int(hash_scenes)))
    g = regenerate(doc, e, sorted(set(order[:m]) | set(pick)), log=log, local=local,
                   skipped=sorted(doc.get("skipped") or []) if m == count else [])
    made = g.pop("scenes")
    out["scenes"] = g
    if im is not None:
        if skip_rerun:
            out["rerun"] = {"image": label(im), "skipped": skip_rerun}
        else:
            todo = {i: made[i] for i in sorted(pick) if i in made}
            out["rerun"] = rerun(doc, im, expert, todo, e, wall_s=wall_s, workers=workers, docker=docker, log=log)
    out["ok"] = not problems and not g["mismatched"] and not g["unverified"]
    return out


def exit_code(out: dict) -> int:
    if out["document"]["problems"] or (out.get("scenes") or {}).get("mismatched"):
        return 1
    return 0 if out["ok"] else 2
