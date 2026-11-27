from __future__ import annotations

import sys
import time


def _err(*x) -> None:
    print(*x, file=sys.stderr, flush=True)


def expert_time(results: list[dict], wall_s: float) -> dict:
    """The expert's own time per pick (vector-env's `expert_s`, the simulation left out) against the per-pick wall
    budget: phase 2 measures it in its re-run and a pick over `wall_s` fails the submission. A pick that ended in
    `wall_timeout` is over, whatever its rounded time."""
    picks = []
    for r in results:
        rows = r.get("picks") if isinstance(r.get("picks"), list) else [r]
        for p in rows:
            x = p.get("expert_s")
            if isinstance(x, (int, float)) and not isinstance(x, bool):
                ended = p.get("outcome") == "wall_timeout" or (p is not r and r.get("outcome") == "wall_timeout"
                                                                and p is rows[-1])
                picks.append((float(x), r.get("scene_id"), p.get("pick", 0), ended))
    over = [{"scene_id": sid, "pick": k, "s": round(x, 3)} for x, sid, k, ended in picks if ended or x >= wall_s]
    return {"wall_s": float(wall_s), "pick_max_s": round(max(x for x, *_ in picks), 3) if picks else None,
            "pick_mean_s": round(sum(x for x, *_ in picks) / len(picks), 3) if picks else None,
            "picks": len(picks), "over": over[:50], "over_scenes": sorted({x["scene_id"] for x in over}, key=str)}


def report_lines(et: dict) -> list[str]:
    if et.get("pick_max_s") is None:
        return []
    if not et["over"]:
        return [f"expert time: at most {et['pick_max_s']:g} s in one pick, mean {et['pick_mean_s']:g} s (the limit in "
                f"phase 2 is the wall budget of a pick, {et['wall_s']:g} s here)"]
    shown = "; ".join(f"{x['scene_id']} pick {x['pick']}: {x['s']:g} s" for x in et["over"][:5])
    return [f"warning: {len(et['over'])} picks used up the wall budget of a pick ({et['wall_s']:g} "
            f"s): {shown}. Phase 2 fails a submission whose expert takes longer than that in one pick (--strict "
            "exits 1 on this)"]


def failed(sc: dict, outcome: str, error: str) -> dict:
    return {"scene_id": sc.get("scene_id"), "outcome": outcome, "success": False, "t": 0.0, "ctrl": "", "n": 0,
            "wall_s": 0.0, "error": error, "infra": True, "action_hash": None}


def _default_limit(rules, cfg: dict) -> float:
    """The time limit of the scenes env.yaml draws: its `task.pile` limit when `draw.mode` is `pile`."""
    mode = "pile" if ((cfg.get("draw") or {}).get("mode") == "pile") else None
    try:
        return float(rules.defaults(mode)["time_limit_s"])
    except TypeError:  # a vector-env without scene modes
        return float(rules.defaults()["time_limit_s"])


def evaluate(expert: dict, n: int, seed: int, *, workers: int = 1, time_limit_s: float | None = None,
             wall_s: float | None = None, docker: dict | None = None, stderr_dir: str | None = None,
             trace_dir: str | None = None, scene_cache_dir=None, log=None, again: int = 0) -> dict:
    from vector_env import harness, rules, scenes
    from vector_env.cli import draw_scenes, scene_cache, write_traces
    from vector_env.settings import env_config
    log = log or _err
    task = rules.task(time_limit_s if time_limit_s is not None else _default_limit(rules, env_config()))
    wall = float(wall_s if wall_s is not None else env_config()["harness"]["wall_s"])
    t0 = time.time()
    cache = scene_cache(scene_cache_dir, None)
    stats = {"cached": 0}
    scs, skipped = draw_scenes(n, seed, None, log, workers=workers, cache=cache, stats=stats)
    gen_s = time.time() - t0

    def show(i, r):
        log(f"{r['scene_id']}: {r['outcome']} t={r['t']} s rows={r['n']} wall={r['wall_s']} s"
            + (f"  ({r['error']})" if r.get("error") else ""))

    t1 = time.time()
    bad = harness.prepare(expert, docker, False)
    runs = []
    if bad is not None:
        res = [failed(sc, bad[0], bad[1]) for sc in scs]
        for i, r in enumerate(res):
            show(i, r)
        runs = [res] * (1 + again)
    else:
        order = scs
        for k in range(1 + again):
            if k:
                log(f"run {k + 1} of {1 + again}: the same {len(scs)} scenes again, in reverse order")
                order = list(reversed(scs))
            got = harness.run(expert, order, time_limit_s=task["time_limit_s"], wall_s=wall, workers=workers,
                              docker=docker, stderr_dir=None if k else stderr_dir,
                              on_result=show if not k else
                              (lambda i, x: log(f"{x['scene_id']}: {x['outcome']} hash={x.get('action_hash')}")))
            runs.append(got)
    res = runs[0]
    out = harness.summary(res)
    out.update(seed=seed, skipped=skipped, seed_tries=scenes.SEED_TRIES, time_limit_s=task["time_limit_s"], wall_s=wall,
               workers=workers, generate_s=round(gen_s, 1), scenes_cached=stats["cached"],
               scene_cache=str(cache.dir) if cache else None, run_s=round(time.time() - t1, 1))
    et = expert_time([r for run in runs for r in run], wall)
    out["expert_time"] = {k: v for k, v in et.items() if k != "over"}
    if trace_dir:
        write_traces(trace_dir, scs, res, time_limit_s=task["time_limit_s"], workers=workers, log=log)
    return {"summary": out, "results": res, "scenes": scs, "runs": runs, "expert_time": et}
