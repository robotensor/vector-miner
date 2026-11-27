from __future__ import annotations

import asyncio
import collections
import datetime as dt
import json
import math
import os
import random
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus, InvalidURI

from vector_miner.api import ApiError
from vector_miner.protocol import hello

from .api import ExpertApi

CONNECT = "/v1/expert/connect"
MAX_MESSAGE = 16 << 20
OPEN_TIMEOUT_S = 10.0
BACKOFF = (1.0, 60.0)
FATAL_HTTP = (400, 401, 403, 404, 410)
BUSY_WAIT_S = 60.0
NONCE_RETRIES = 5


class ServeError(Exception):
    pass


def _log(*x) -> None:
    print("vector-miner:", *x, file=sys.stderr, flush=True)


def ws_url(api: str) -> str:
    u = api.rstrip("/")
    for a, b in (("https://", "wss://"), ("http://", "ws://")):
        if u.startswith(a):
            return b + u[len(a):] + CONNECT
    raise ServeError(f"--api must start with http:// or https:// (got {api!r})")


def refused(code: int | None) -> bool:
    return code is not None and (code == 1008 or 4000 <= code <= 4999)


def _when(v) -> dt.datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        t = dt.datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def refusal(code: int | None, close_reason: str = "", error: dict | None = None, now: dt.datetime | None = None) -> dict:
    err = error if isinstance(error, dict) else {}
    head, _, tail = (close_reason or "").partition(": ")
    if head and " " not in head:
        said, text = head, tail
    else:
        said, text = "", close_reason or ""
    reason = str(err.get("reason") or said)
    message = str(err.get("message") or err.get("error") or text)
    out = {"code": code, "reason": reason, "message": message, "final": refused(code), "wait_s": None,
           "retry_at": None, "nonce": False}
    if not out["final"]:
        return out
    if code == 4429 or reason == "busy":
        out.update(final=False, wait_s=_num(err.get("retry_after_s")) or BUSY_WAIT_S)
    elif reason in ("not_open", "not_running") and _when(err.get("retry_at")) is not None:
        at = _when(err["retry_at"])
        left = (at - (now or dt.datetime.now(dt.timezone.utc))).total_seconds()
        out.update(final=False, wait_s=max(0.0, left), retry_at=err["retry_at"])
    elif code == 4408 or reason == "timeout":
        out["final"] = False
    elif (code == 4401 or reason == "bad_hello") and "nonce" in message.lower() and "signature" not in message.lower():
        out.update(final=False, nonce=True)
    return out


def fatal_close(code: int | None, close_reason: str = "", error: dict | None = None) -> bool:
    return refusal(code, close_reason, error)["final"]


def _says(m: dict) -> str:
    r, msg = m.get("reason"), m.get("message") or m.get("error")
    return ": ".join(str(x) for x in (r, msg) if x) or "no reason given"


def _wait_text(s: float) -> str:
    if s >= 3600:
        return f"{s / 3600:.1f} h"
    if s >= 60:
        return f"{s / 60:.1f} min"
    return f"{s:.1f} s"


def _signed(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _sc(v) -> str:
    x = _signed(v)
    if x is None:
        return "null"
    x = round(x, 3)
    return "0.000" if x == 0 else f"{x:+.3f}"


CHILD = ("import sys; from vector_miner.skills.bin_picking.expert.serve import child; sys.exit(child())")


def play(job: dict) -> dict:
    """Runs one scene with vector-env's harness on this machine (the simulator and the expert) and returns
    `{"result": <the claim>, "errors": <check_claim's lines>}`. job: {expert, scene, limits, docker, stderr_dir,
    index}."""
    from vector_env import assets, harness
    from vector_env.settings import env_config
    sc, lim = job["scene"], job["limits"]
    t0 = time.monotonic()
    try:
        bundle = assets.for_scene(sc) if hasattr(assets, "for_scene") else None
        wall = float(lim.get("wall_s") or env_config()["harness"]["wall_s"])
        r = harness.run(job["expert"], [sc], time_limit_s=float(lim["time_limit_s"]), wall_s=wall,
                        docker=job.get("docker"), stderr_dir=job.get("stderr_dir"), bundle=bundle)[0]
    except Exception as e:
        r = failed(sc, f"{type(e).__name__}: {e}"[:500], time.monotonic() - t0)
    d = job.get("stderr_dir")
    if d:
        try:
            os.replace(harness.stderr_path(d, 0, sc), harness.stderr_path(d, int(job.get("index") or 0), sc))
        except OSError:
            pass
    claim = harness.wire_result(r, lim)
    return {"result": claim, "errors": harness.check_claim(sc, claim, lim)}


def failed(sc: dict, error: str, wall: float = 0.0) -> dict:
    """A scene this machine could not run (no bundle, a broken vector-env): an infra result with no actions."""
    return {"scene_id": sc.get("scene_id"), "outcome": "harness_error", "success": False, "error": error, "infra": True,
            "action_hash": None, "rows_sent": 0, "t": 0.0,
            "tick": 0, "outs": 0, "out_parts": [], "tcp_path_m": 0.0, "joint_path_rad": 0.0, "crate_hits": 0,
            "target_class": sc.get("target_class"), "score": None, "expert_s": 0.0, "wall_s": round(wall, 3)}


def child() -> int:
    """The process of one scene: a job (JSON) on stdin, `play`'s answer (one JSON line) on stdout."""
    job = json.loads(sys.stdin.buffer.read())
    out = play(job)
    sys.stdout.write(json.dumps(out, separators=(",", ":"), allow_nan=False) + "\n")
    sys.stdout.flush()
    return 0


def _summary(r: dict) -> str:
    o = r.get("outcome")
    if isinstance(r.get("picks"), list):
        bits = f"{r.get('successes', 0)}/{r.get('picks_planned') or len(r['picks'])} picks, {r.get('outs', 0)} out"
        return f"{o} ({bits}, t={r.get('t')} s, expert {r.get('expert_s')} s)"
    return f"{o} t={r.get('t')} s, expert {r.get('expert_s')} s"


class Runner:
    """Phase 1 on this machine: runs every scene the server sends (one process per scene: vector-env's harness, the
    simulator and the expert's container), checks its claim with vector-env's check_claim and answers with it. Runs
    and answers outlive a connection: after a reconnect the answers not yet acknowledged are sent again, and a scene
    sent again is not run again."""

    def __init__(self, expert: dict, docker: dict | None = None, stderr_dir: str | Path | None = None,
                 cap: int | None = None, log=_log, argv: list[str] | None = None):
        if not expert.get("command") and not expert.get("image"):
            raise ServeError("the expert needs an image or a command")
        self.expert, self.log = expert, log
        self.argv = list(argv) if argv else [sys.executable, "-c", CHILD]
        self.tag = f"vector-miner-{uuid.uuid4().hex[:12]}"
        self.docker = dict(docker or {})
        if expert.get("image") and not expert.get("command"):
            self.docker["args"] = [*(self.docker.get("args") or []), "--label", f"vector-miner.serve={self.tag}"]
        self.stderr_dir = str(stderr_dir) if stderr_dir else None
        if self.stderr_dir:
            Path(self.stderr_dir).mkdir(parents=True, exist_ok=True)
        self.sem = asyncio.Semaphore(int(cap)) if cap else None
        self.cap = int(cap) if cap else None
        self.tasks: dict[int, asyncio.Task] = {}
        self.procs: dict[int, asyncio.subprocess.Process] = {}
        self.ids: dict[int, object] = {}
        self.claims: dict[int, dict] = {}
        self.unacked: set[int] = set()
        self.acks: dict[int, dict] = {}
        self.out: asyncio.Queue | None = None
        self.outcomes: collections.Counter = collections.Counter()
        self.total: int | None = None
        self.server_done: int = 0
        self.done: dict | None = None
        self.failed_checks = 0
        self.closed = False

    @property
    def answered(self) -> int:
        return len(self.claims)

    def claimed(self) -> float | None:
        xs = [c["score"] for c in self.claims.values() if _signed(c.get("score")) is not None]
        return sum(xs) / len(xs) if xs else None

    def progress(self) -> str:
        tot = self.total if self.total is not None else "?"
        line = f"{self.server_done + self.answered}/{tot} scenes answered" if self.total is not None else \
            f"{self.answered} scenes answered here"
        acc = sum(1 for a in self.acks.values() if a.get("ok"))
        rej = sum(1 for a in self.acks.values() if a.get("ok") is False)
        line += f", claimed score {_sc(self.claimed())} over {self.answered} here"
        if self.acks:
            line += f" ({acc} accepted, {rej} rejected)"
        return line

    async def on_scene(self, m: dict) -> None:
        idx, sc, lim = m.get("index"), m.get("scene"), m.get("limits")
        if not (isinstance(idx, int) and not isinstance(idx, bool) and idx >= 0 and isinstance(sc, dict)
                and isinstance(lim, dict)):
            self.log(f"ignored a scene message without index, scene and limits: {str(m)[:200]}")
            return
        sid = sc.get("scene_id")
        if self.ids.get(idx) == sid and (idx in self.claims or idx in self.tasks):
            if idx in self.claims:
                self.unacked.add(idx)
                self._push(idx)
            return
        self.ids[idx] = sid
        self.claims.pop(idx, None)
        self.unacked.discard(idx)
        self.acks.pop(idx, None)
        self.log(f"scene {idx} ({sid}): running here")
        self.tasks[idx] = asyncio.ensure_future(self._run(idx, sc, lim))

    async def _run(self, idx: int, sc: dict, lim: dict) -> None:
        try:
            if self.sem is not None:
                async with self.sem:
                    got = await self._child(idx, sc, lim)
            else:
                got = await self._child(idx, sc, lim)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            got = {"result": failed(sc, f"serve could not run the scene: {type(e).__name__}: {e}"[:500]), "errors": []}
        finally:
            self.tasks.pop(idx, None)
        if self.ids.get(idx) != sc.get("scene_id"):
            return
        r, errs = got["result"], got.get("errors") or []
        self.claims[idx] = r
        self.unacked.add(idx)
        self.outcomes[str(r.get("outcome"))] += 1
        self.log(f"scene {idx} ({sc.get('scene_id')}): {_summary(r)}, claimed score {_sc(r.get('score'))}"
                 + (f" ({r['error']})" if r.get("error") else "") + f"  [{self.progress()}]")
        if errs:
            self.failed_checks += 1
            self.log(f"warning: scene {idx}: this claim fails the subnet's check (it scores 0): " + "; ".join(errs[:3]))
        self._push(idx)

    async def _child(self, idx: int, sc: dict, lim: dict) -> dict:
        job = {"expert": self.expert, "scene": sc, "limits": lim, "docker": self.docker, "stderr_dir": self.stderr_dir,
               "index": idx}
        p = await asyncio.create_subprocess_exec(*self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                 stderr=subprocess.PIPE, start_new_session=True, limit=MAX_MESSAGE)
        self.procs[idx] = p
        try:
            out, err = await p.communicate(json.dumps(job, separators=(",", ":")).encode())
        finally:
            self.procs.pop(idx, None)
        if p.returncode != 0:
            tail = err.decode(errors="replace").strip()[-300:]
            raise ServeError(f"the scene's process exited {p.returncode}" + (f": {tail}" if tail else ""))
        return json.loads(out)

    def running(self) -> int:
        return len(self.tasks)

    def _push(self, idx: int) -> None:
        if self.out is not None and idx in self.claims:
            self.out.put_nowait({"type": "result", "index": idx, "result": self.claims[idx]})

    def on_ack(self, m: dict) -> None:
        idx = m.get("index")
        if idx not in self.claims:
            return
        self.unacked.discard(idx)
        self.acks[idx] = m
        if m.get("ok") is False:
            self.log(f"scene {idx}: the server rejected this answer (the scene scores 0): {m.get('reason') or 'no reason given'}")

    async def connection(self, ws, on_message=None) -> str:
        out: asyncio.Queue = asyncio.Queue()
        self.out = out

        async def writer() -> None:
            while True:
                m = await out.get()
                await ws.send(json.dumps(m, separators=(",", ":"), allow_nan=False))

        w = asyncio.ensure_future(writer())
        try:
            while True:
                recv = asyncio.ensure_future(ws.recv())
                done, _ = await asyncio.wait({recv, w}, return_when=asyncio.FIRST_COMPLETED)
                if w in done:
                    recv.cancel()
                    w.result()
                raw = recv.result()
                try:
                    m = json.loads(raw)
                except (ValueError, TypeError, RecursionError):
                    continue
                if not isinstance(m, dict):
                    continue
                if on_message:
                    on_message(m)
                t = m.get("type")
                if t == "scene":
                    await self.on_scene(m)
                elif t == "ack":
                    self.on_ack(m)
                elif t == "welcome":
                    self.log(_welcome(m))
                    why = mismatch(m)
                    if why:
                        self.log(why)
                        return "mismatch"
                    total, done_n = m.get("scenes", m.get("total")), m.get("done")
                    if isinstance(total, int) and not isinstance(total, bool):
                        self.total = total
                    want = _num(m.get("scenes_at_once"))
                    if want is not None and self.cap is not None and want > self.cap:
                        self.log(f"warning: the server sends {want:g} scenes at once and --concurrency caps this miner "
                                 f"at {self.cap}: the extra scenes wait here and may miss their deadline")
                    for idx in sorted(self.unacked):
                        self._push(idx)
                    if self.unacked:
                        self.log(f"sent {len(self.unacked)} answers again that the server had not acknowledged")
                    if isinstance(done_n, int) and not isinstance(done_n, bool):
                        self.server_done = max(0, done_n - len(self.claims))
                elif t == "done":
                    self.done = m
                    return "done"
                elif t == "error":
                    if m.get("index") is not None:
                        self.log(f"scene {m.get('index')}: the server says: {_says(m)}")
                    else:
                        self.log(f"the server says: {_says(m)}")
        finally:
            self.out = None
            w.cancel()
            try:
                await w
            except (asyncio.CancelledError, Exception):
                pass

    async def close(self) -> None:
        """Stops every run: kills each scene's process group and the containers this miner started."""
        self.closed = True
        for t in list(self.tasks.values()):
            t.cancel()
        if self.expert.get("image") and not self.expert.get("command"):
            await _kill_labelled(str(self.docker.get("binary") or "docker"), f"vector-miner.serve={self.tag}")
        for p in list(self.procs.values()):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        for t in list(self.tasks.values()):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        for p in list(self.procs.values()):
            try:
                await asyncio.wait_for(p.wait(), 10)
            except (asyncio.TimeoutError, Exception):
                pass


async def _kill_labelled(binary: str, label: str) -> None:
    try:
        p = await asyncio.create_subprocess_exec(binary, "ps", "-q", "--filter", f"label={label}",
                                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 30)
        ids = out.decode().split()
        if ids:
            k = await asyncio.create_subprocess_exec(binary, "kill", *ids, stdout=subprocess.DEVNULL,
                                                     stderr=subprocess.DEVNULL)
            await asyncio.wait_for(k.wait(), 30)
    except (OSError, asyncio.TimeoutError):
        pass

def local_pin() -> dict:
    """This machine's vector-env version and vector-sim code pin (what it runs phase 1 with)."""
    import vector_env
    from vector_env.skills.bin_picking import verifier
    return {"version": getattr(vector_env, "__version__", None), "sim": (verifier.pin() or {}).get("code")}


def mismatch(m: dict) -> str | None:
    """Why this vector-miner must not play the submission the welcome names: its phase 1 is of another kind, or this
    machine runs another vector-env or vector-sim than the subnet re-runs claims with."""
    mode = m.get("phase1")
    if mode is not None and mode != "scene":
        return (f"this submission's phase 1 runs in {mode} mode, which this vector-miner does not play: run the "
                "vector-miner of the round you committed in (its round tag)")
    want = m.get("env") or {}
    if not (want.get("version") or want.get("sim")):
        return None
    try:
        have = local_pin()
    except Exception as e:
        return f"this machine's vector-env / vector-sim cannot be read ({type(e).__name__}: {e}); run scripts/setup.sh"
    for k, name in (("version", "vector-env version"), ("sim", "vector-sim code pin")):
        if want.get(k) and have.get(k) != want.get(k):
            return (f"this machine has {name} {have.get(k)!r}, the subnet re-runs claims with {want.get(k)!r}: "
                    "set up the round tag (scripts/setup.sh) before serving, or phase 2 cannot reproduce your claims")
    return None


def _welcome(m: dict) -> str:
    line = "connected"
    if m.get("submission") is not None:
        line += f": submission {m['submission']}"
    if m.get("status") is not None:
        line += f", status {m['status']}"
    total = m.get("scenes", m.get("total"))
    if m.get("done") is not None or total is not None:
        line += f", {m.get('done') if m.get('done') is not None else '?'}/{total if total is not None else '?'} scenes done"
    conc, dl = _num(m.get("scenes_at_once")), _num(m.get("scene_deadline_s"))
    if conc is not None:
        line += f"; the server sends {conc:g} scene{'s' if conc != 1 else ''} at a time"
    if dl is not None:
        line += f"; each scene must be answered within {_wait_text(dl)}"
    return line


def _done(m: dict) -> str:
    line = "the server says phase 1 is finished"
    if m.get("status") is not None:
        line += f": status {m['status']}"
    if _signed(m.get("score")) is not None:
        line += f", score {_sc(m['score'])}"
        if m.get("successes") is not None and m.get("scenes") is not None:
            line += f" ({m['successes']}/{m['scenes']} successes)"
    if m.get("detail"):
        line += f" ({m['detail']})"
    return line


async def _sleep_or_stop(stop: asyncio.Event, s: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), s)
    except asyncio.TimeoutError:
        pass


async def serve(api: str, image: str, keypair, runner: Runner, *, stop: asyncio.Event | None = None,
                backoff: tuple[float, float] = BACKOFF, log=_log, max_attempts: int | None = None) -> int:
    url = ws_url(api)
    stop = stop or asyncio.Event()
    runner.log = log
    delay, attempts, nonce_refusals = backoff[0], 0, 0
    try:
        while not stop.is_set():
            attempts += 1
            accepted = {"ok": False, "error": None}

            def seen(m: dict) -> None:
                if m.get("type") == "welcome":
                    accepted["ok"] = True
                elif m.get("type") == "error" and m.get("index") is None:
                    accepted["error"] = m

            why, wait = None, None
            try:
                nonce = await asyncio.to_thread(ExpertApi(api, timeout=OPEN_TIMEOUT_S).nonce)
                async with ws_connect(url, max_size=MAX_MESSAGE, open_timeout=OPEN_TIMEOUT_S, ping_interval=20,
                                      ping_timeout=20, close_timeout=5) as ws:
                    await ws.send(json.dumps(hello(keypair, image, nonce), separators=(",", ":")))
                    run = asyncio.ensure_future(runner.connection(ws, seen))
                    halt = asyncio.ensure_future(stop.wait())
                    done, _ = await asyncio.wait({run, halt}, return_when=asyncio.FIRST_COMPLETED)
                    if run in done:
                        halt.cancel()
                        if run.result() == "mismatch":
                            await ws.close(1000, "this miner cannot play this submission")
                            return 1
                        if run.result() == "done":
                            await ws.close(1000, "phase 1 finished")
                            m = runner.done or {}
                            log(_done(m))
                            r = refusal(4403, "", {"reason": "not_open", "retry_at": m.get("retry_at")}) \
                                if m.get("status") == "review" else None
                            if r is None or r["final"]:
                                log(f"done: {runner.answered} scenes answered here ({_counts(runner)})")
                                return 0
                            wait = r["wait_s"]
                            why = (f"scenes failed on the server's side and phase 1 runs again from {r['retry_at']}; "
                                   "keep serve running")
                    else:
                        run.cancel()
                        try:
                            await run
                        except (asyncio.CancelledError, Exception):
                            pass
                        await ws.close(1000, "the miner is shutting down")
                        break
            except ConnectionClosed as e:
                code, reason = (e.rcvd.code, e.rcvd.reason) if e.rcvd else (None, "")
                r = refusal(code, reason, accepted["error"])
                said = ": ".join(x for x in (r["reason"], r["message"]) if x) or "no reason given"
                nonce_refusals = nonce_refusals + 1 if r["nonce"] else 0
                if r["final"] or nonce_refusals > NONCE_RETRIES:
                    log(f"the server refused this miner (close {code}): {said}")
                    return 1
                if refused(code):
                    why = f"the server turned the connection away for now (close {code}: {said})"
                    wait = r["wait_s"]
                    if r["retry_at"]:
                        why += f"; it opens again at {r['retry_at']}"
                else:
                    why = f"the connection closed ({code if code is not None else 'no close frame'}"
                    why += f": {reason})" if reason else ")"
            except InvalidStatus as e:
                st = e.response.status_code
                if st in FATAL_HTTP:
                    body = (e.response.body or b"").decode(errors="replace")[:300]
                    log(f"the server refused the connection (HTTP {st}){': ' + body if body else ''}")
                    return 1
                why = f"the server answered HTTP {st}"
            except ApiError as e:
                if e.status in FATAL_HTTP:
                    log(f"the server refused the nonce request: {e}")
                    return 1
                why = f"the nonce request failed: {e}"
            except InvalidURI as e:
                raise ServeError(str(e)) from None
            except (OSError, httpx.HTTPError, InvalidHandshake, asyncio.TimeoutError) as e:
                why = f"cannot connect: {type(e).__name__}: {e}"
            if accepted["ok"]:
                delay = backoff[0]
            if stop.is_set():
                break
            if max_attempts is not None and attempts >= max_attempts:
                log(f"{why}; giving up after {attempts} attempts")
                return 2
            if wait is not None:
                wait = max(backoff[0], wait) * (1 + random.random() / 10)
                log(f"{why}; connecting again in {_wait_text(wait)}")
                await _sleep_or_stop(stop, wait)
                continue
            wait = min(backoff[1], delay) * (0.5 + random.random() / 2)
            running = runner.running()
            log(f"{why}; reconnecting in {wait:.1f} s" + (f" ({running} scenes keep running here; their answers are "
                                                          "sent after the reconnect)" if running else ""))
            await _sleep_or_stop(stop, wait)
            delay = min(backoff[1], delay * 2)
        log(f"stopped: {runner.answered} scenes answered here ({_counts(runner)})")
        return 0
    finally:
        await runner.close()


def _counts(r: Runner) -> str:
    bits = ", ".join(f"{k} {v}" for k, v in sorted(r.outcomes.items())) or "none"
    return f"{bits}; claimed score {_sc(r.claimed())}"


def run(api: str, image: str, keypair, runner: Runner, **kw) -> int:
    async def main() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(s, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        return await serve(api, image, keypair, runner, stop=stop, **kw)

    return asyncio.run(main())
