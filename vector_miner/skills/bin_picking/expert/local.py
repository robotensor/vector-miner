from __future__ import annotations

import asyncio
import inspect
import json
import sys
import threading
import time

from .serve import Bridge, Launcher, Warm

FIRST_STEP_DEADLINE_S = 10.0
STEP_DEADLINE_S = 2.0
CLOSE_S = 120.0


def _err(*x) -> None:
    print(*x, file=sys.stderr, flush=True)


def deadlines(first: float | None = None, step: float | None = None) -> dict:
    try:
        from vector_env.settings import env_config
        r = env_config()["harness"].get("remote") or {}
    except Exception:
        r = {}
    f = first if first is not None else float(r.get("first_step_deadline_s") or FIRST_STEP_DEADLINE_S)
    s = step if step is not None else float(r.get("step_deadline_s") or STEP_DEADLINE_S)
    return {"first_step_deadline_s": float(f), "step_deadline_s": float(s)}


def harness_takes_first(harness) -> bool:
    try:
        return "first_step_deadline_s" in inspect.signature(harness.arun).parameters
    except (TypeError, ValueError, AttributeError):
        return False


class _End:
    def __init__(self, inbox: asyncio.Queue, outbox: asyncio.Queue, encode: bool):
        self.inbox, self.outbox, self.encode = inbox, outbox, encode

    async def send(self, msg) -> None:
        self.outbox.put_nowait(json.dumps(msg, separators=(",", ":")) if self.encode else msg)

    async def recv(self):
        m = await self.inbox.get()
        return json.loads(m) if self.encode else m


class Local:
    def __init__(self, launcher: Launcher, concurrency: int, warm: int | None = None):
        self.launcher, self.concurrency = launcher, max(1, int(concurrency))
        self.warm = self.concurrency if warm is None else max(0, int(warm))
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "Local":
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="vector-miner-local", daemon=True)
        self.thread.start()
        try:
            self._call(self._open())
        except BaseException:
            self._stop_loop()
            raise
        return self

    def __exit__(self, *exc) -> None:
        try:
            self._call(self._close(), CLOSE_S + 30)
        finally:
            self._stop_loop()

    def _call(self, coro, timeout: float | None = None):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def _stop_loop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(30)
        self.loop.close()

    async def _open(self) -> None:
        to_bridge, to_harness = asyncio.Queue(), asyncio.Queue()
        self.channel = _End(to_harness, to_bridge, True)
        self.spare = Warm(self.launcher, self.warm)
        self.bridge = Bridge(self.spare, self.concurrency, log=lambda *x: None, timing=True)
        self._to_bridge, self._to_harness = to_bridge, to_harness
        await self.spare.fill()
        self.task = asyncio.ensure_future(self.bridge.connection(_End(to_bridge, to_harness, False)))

    async def _close(self) -> None:
        self._to_bridge.put_nowait(json.dumps({"type": "done"}))
        try:
            await asyncio.wait_for(asyncio.shield(self.task), 10)
        except (asyncio.TimeoutError, Exception):
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        await asyncio.wait_for(self.bridge.drain(), CLOSE_S)
        await self.spare.close()

    def run(self, scenes: list[dict], *, time_limit_s: float, wall_s: float, limits: dict, on_result=None) -> list[dict]:
        from vector_env import harness
        first, step = limits["first_step_deadline_s"], limits["step_deadline_s"]
        kw = {"step_deadline_s": step}
        if harness_takes_first(harness):
            kw["first_step_deadline_s"] = first
        else:
            kw["step_deadline_s"] = max(first, step)
        self.enforced = {"first_act_s": first, "later_acts_s": kw["step_deadline_s"]}
        self._call(self._reset())
        return self._call(harness.arun({"remote": self.channel}, scenes, time_limit_s=time_limit_s, wall_s=wall_s,
                                       workers=self.concurrency, max_restarts=0, on_result=on_result, **kw))

    async def _reset(self) -> None:
        while not self._to_harness.empty():
            self._to_harness.get_nowait()
        self.bridge.timing.clear()

    def latency(self, results: list[dict], limits: dict) -> dict:
        return latency(self.bridge.timing, results, limits, getattr(self, "enforced", None))


def latency(timing: dict, results: list[dict], limits: dict, enforced: dict | None = None) -> dict:
    first_d, step_d = limits["first_step_deadline_s"], limits["step_deadline_s"]
    firsts, later, late = [], [], []
    by_id: dict = {}
    for key, t in sorted(timing.items(), key=lambda kv: _index(kv[0])):
        acts = t.get("acts") or []
        sid = t.get("scene_id")
        by_id[sid] = t
        if acts:
            firsts.append(acts[0])
            if acts[0] > first_d:
                late.append({"scene_id": sid, "act": 1, "s": round(acts[0], 3), "deadline_s": first_d})
        for j, x in enumerate(acts[1:], 2):
            later.append(x)
            if x > step_d:
                late.append({"scene_id": sid, "act": j, "s": round(x, 3), "deadline_s": step_d})
        p = t.get("pending")
        if p is not None:
            d = first_d if not acts else step_d
            if p > d:
                late.append({"scene_id": sid, "act": len(acts) + 1, "s": round(p, 3), "deadline_s": d,
                             "unanswered": True})
    timeouts = [r.get("scene_id") for r in results if r.get("outcome") == "step_timeout"]
    out = {"first_step_deadline_s": first_d, "step_deadline_s": step_d,
           "first_act_max_s": round(max(firsts), 3) if firsts else None,
           "first_act_mean_s": round(sum(firsts) / len(firsts), 3) if firsts else None,
           "later_act_max_s": round(max(later), 3) if later else None,
           "late_acts": len(late), "late_scenes": sorted({x["scene_id"] for x in late} | set(timeouts), key=str),
           "step_timeouts": len(timeouts), "late": late[:50]}
    if enforced is not None:
        out["enforced"] = enforced
    return out


def _index(key: str) -> tuple:
    a, _, b = str(key).partition(".")
    try:
        return int(a), int(b or 0)
    except ValueError:
        return 1 << 62, 0


def report_lines(lat: dict) -> list[str]:
    f, s = lat["first_step_deadline_s"], lat["step_deadline_s"]
    if not lat["late_scenes"]:
        if lat.get("first_act_max_s") is None:
            return []
        return [f"deadlines met: first act at most {lat['first_act_max_s']:g} s (limit {f:g} s), later acts at most "
                f"{lat['later_act_max_s'] or 0:g} s (limit {s:g} s)"]
    shown = []
    for x in lat["late"][:5]:
        what = "no answer after" if x.get("unanswered") else "answered after"
        shown.append(f"{x['scene_id']} act {x['act']} {what} {x['s']:g} s > {x['deadline_s']:g} s")
    lines = [f"warning: {len(lat['late_scenes'])} scenes missed a phase-1 deadline (first act {f:g} s, then {s:g} s "
             f"per act)" + (f": {'; '.join(shown)}" if shown else "")
             + (f"; {lat['step_timeouts']} ended step_timeout here" if lat["step_timeouts"] else "")]
    enf = lat.get("enforced") or {}
    if enf and enf.get("later_acts_s", s) > s:
        lines.append(f"warning: this vector-env holds every act to {enf['later_acts_s']:g} s, so a later act slower "
                     f"than {s:g} s did not end its scene here; phase 1 ends it as step_timeout")
    lines.append("phase 1 ends a scene as step_timeout (a failure) when an act is late: answer start within "
                 f"{f:g} s and every state within {s:g} s, with your plan split across acts (--strict exits 1 on this)")
    return lines


def failed(sc: dict, outcome: str, error: str) -> dict:
    return {"scene_id": sc.get("scene_id"), "outcome": outcome, "success": False, "t": 0.0, "ctrl": "", "n": 0,
            "wall_s": 0.0, "error": error, "infra": True, "action_hash": None}


def evaluate(expert: dict, n: int, seed: int, *, workers: int = 1, time_limit_s: float | None = None,
             wall_s: float | None = None, docker: dict | None = None, stderr_dir: str | None = None,
             trace_dir: str | None = None, scene_cache_dir=None, limits: dict | None = None, log=None,
             again: int = 0) -> dict:
    from vector_env import assets, harness, rules, scenes
    from vector_env.cli import draw_scenes, scene_cache, write_traces
    from vector_env.settings import env_config
    log = log or _err
    limits = limits or deadlines()
    task = rules.task(time_limit_s if time_limit_s is not None else rules.defaults()["time_limit_s"])
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
    runs, lats = [], []
    if bad is not None:
        res = [failed(sc, bad[0], bad[1]) for sc in scs]
        for i, r in enumerate(res):
            show(i, r)
        runs = [res] * (1 + again)
    else:
        bundle = assets.ensure()
        with Local(Launcher(expert, docker, bundle, stderr_dir, by_scene=True), workers) as lc:
            order = scs
            for k in range(1 + again):
                if k:
                    log(f"run {k + 1} of {1 + again}: the same {len(scs)} scenes again, in reverse order")
                    order = list(reversed(scs))
                got = lc.run(order, time_limit_s=task["time_limit_s"], wall_s=wall, limits=limits,
                             on_result=show if not k else
                             (lambda i, x: log(f"{x['scene_id']}: {x['outcome']} hash={x.get('action_hash')}")))
                runs.append(got)
                lats.append(lc.latency(got, limits))
    res = runs[0]
    out = harness.summary(res)
    out.update(seed=seed, skipped=skipped, seed_tries=scenes.SEED_TRIES, time_limit_s=task["time_limit_s"], wall_s=wall,
               workers=workers, generate_s=round(gen_s, 1), scenes_cached=stats["cached"],
               scene_cache=str(cache.dir) if cache else None, run_s=round(time.time() - t1, 1))
    lat = merge(lats, limits) if lats else latency({}, res, limits)
    out["deadlines"] = {k: v for k, v in lat.items() if k != "late"}
    if trace_dir:
        write_traces(trace_dir, scs, res, time_limit_s=task["time_limit_s"], workers=workers, log=log)
    return {"summary": out, "results": res, "scenes": scs, "runs": runs, "latency": lat}


def merge(lats: list[dict], limits: dict) -> dict:
    if len(lats) == 1:
        return lats[0]
    out = dict(lats[0])
    for k in ("first_act_max_s", "later_act_max_s"):
        xs = [x[k] for x in lats if x.get(k) is not None]
        out[k] = max(xs) if xs else None
    out["late"] = [y for x in lats for y in x["late"]][:50]
    out["late_acts"] = sum(x["late_acts"] for x in lats)
    out["step_timeouts"] = sum(x["step_timeouts"] for x in lats)
    out["late_scenes"] = sorted({s for x in lats for s in x["late_scenes"]}, key=str)
    return out
