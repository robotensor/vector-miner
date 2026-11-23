from __future__ import annotations

import asyncio
import collections
import datetime as dt
import json
import os
import random
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus, InvalidURI

from .api import Api, ApiError
from .protocol import hello

CONNECT = "/v1/expert/connect"
MAX_LINE = 1 << 20
MAX_MESSAGE = 64 << 20
OPEN_TIMEOUT_S = 10.0
END_GRACE_S = 2.0
IDLE_S = 300.0
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
    elif reason == "not_open" and _when(err.get("retry_at")) is not None:
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


class Launcher:
    def __init__(self, expert: dict, docker: dict | None = None, bundle: Path | None = None,
                 stderr_dir: str | Path | None = None, by_scene: bool = False):
        if not expert.get("command") and not expert.get("image"):
            raise ServeError("the expert needs an image or a command")
        self.expert, self.docker, self.bundle = expert, docker, bundle
        self.stderr_dir = Path(stderr_dir) if stderr_dir else None
        self.by_scene = by_scene
        if self.stderr_dir:
            self.stderr_dir.mkdir(parents=True, exist_ok=True)

    def spec(self) -> dict:
        name = f"vector-miner-{uuid.uuid4().hex[:12]}"
        if self.expert.get("command"):
            cmd = self.expert["command"]
            argv = shlex.split(cmd) if isinstance(cmd, str) else [str(x) for x in cmd]
            env = dict(os.environ)
            if self.bundle is not None:
                env["VECTOR_SIM_BUNDLE"] = str(self.bundle)
            return {"name": name, "argv": argv, "kill": None, "env": env, "cwd": str(self.expert.get("cwd") or Path.cwd())}
        from vector_env import harness
        argv, kill = harness.docker_argv(str(self.expert["image"]), name, self.docker, self.bundle)
        return {"name": name, "argv": argv, "kill": kill, "env": None, "cwd": None}

    def label(self, ex: "Expert", key: str, start: dict) -> None:
        if not (self.stderr_dir and self.by_scene):
            return
        try:
            idx = int(str(key).split(".")[0])
        except ValueError:
            return
        sid = re.sub(r"[^A-Za-z0-9._-]", "_", str((start.get("scene") or {}).get("scene_id") or "scene"))[:120]
        try:
            os.replace(self.stderr_dir / f"{ex.name}.log", self.stderr_dir / f"{idx:04d}-{sid}.log")
        except OSError:
            pass

    def discard(self, ex: "Expert") -> None:
        if self.stderr_dir and self.by_scene:
            try:
                (self.stderr_dir / f"{ex.name}.log").unlink()
            except OSError:
                pass

    async def spawn(self) -> Expert:
        sp = self.spec()
        err = open(self.stderr_dir / f"{sp['name']}.log", "wb") if self.stderr_dir else subprocess.DEVNULL
        try:
            proc = await asyncio.create_subprocess_exec(*sp["argv"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                        stderr=err, env=sp["env"], cwd=sp["cwd"], limit=MAX_LINE + 2,
                                                        start_new_session=True)
        finally:
            if err is not subprocess.DEVNULL:
                err.close()
        return Expert(sp, proc)


class Expert:
    def __init__(self, spec: dict, proc: asyncio.subprocess.Process):
        self.spec, self.proc, self.stopped = spec, proc, False

    @property
    def name(self) -> str:
        return self.spec["name"]

    def alive(self) -> bool:
        return self.proc.returncode is None

    async def write(self, msg: dict) -> bool:
        try:
            self.proc.stdin.write((json.dumps(msg, separators=(",", ":")) + "\n").encode())
            await self.proc.stdin.drain()
            return True
        except (BrokenPipeError, ConnectionResetError, RuntimeError, AttributeError):
            return False

    async def readline(self) -> bytes | None:
        try:
            line = await self.proc.stdout.readline()
        except ValueError:
            raise ServeError(f"the expert wrote a line longer than {MAX_LINE} bytes") from None
        return line or None

    async def stop(self, grace: float = END_GRACE_S) -> None:
        if self.stopped:
            return
        self.stopped = True
        try:
            if self.proc.stdin and not self.proc.stdin.is_closing():
                self.proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass
        if grace > 0 and self.alive():
            try:
                await asyncio.wait_for(self.proc.wait(), grace)
            except asyncio.TimeoutError:
                pass
        if self.alive():
            await self._kill()
        try:
            await asyncio.wait_for(self.proc.wait(), 30)
        except asyncio.TimeoutError:
            pass

    async def _kill(self) -> None:
        if self.spec.get("kill"):
            try:
                k = await asyncio.create_subprocess_exec(*self.spec["kill"], stdout=subprocess.DEVNULL,
                                                         stderr=subprocess.DEVNULL)
                await asyncio.wait_for(k.wait(), 30)
            except (OSError, asyncio.TimeoutError):
                pass
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass


class Pool:
    def __init__(self, launcher: Launcher, size: int):
        self.launcher, self.size = launcher, max(0, int(size))
        self.ready: collections.deque[Expert] = collections.deque()
        self.filling: asyncio.Task | None = None
        self.closed = False
        self.spawned = 0

    async def _spawn(self) -> Expert:
        self.spawned += 1
        return await self.launcher.spawn()

    async def fill(self) -> None:
        while not self.closed and len(self.ready) < self.size:
            e = await self._spawn()
            if self.closed:
                await e.stop(0)
                return
            self.ready.append(e)

    def _refill(self) -> None:
        if self.size and not self.closed and (self.filling is None or self.filling.done()):
            self.filling = asyncio.ensure_future(self.fill())

    async def take(self) -> Expert:
        while self.ready:
            e = self.ready.popleft()
            if e.alive():
                self._refill()
                return e
            await e.stop(0)
        self._refill()
        return await self._spawn()

    async def close(self) -> None:
        self.closed = True
        if self.filling is not None:
            try:
                await self.filling
            except Exception:
                pass
        while self.ready:
            e = self.ready.popleft()
            await e.stop(0)
            self.launcher.discard(e)


def _plain(msg: dict) -> dict:
    return {k: v for k, v in msg.items() if k != "scene_key"}


class Bridge:
    def __init__(self, pool: Pool, concurrency: int, log=_log, idle_s: float = IDLE_S, timing: bool = False):
        self.pool, self.log, self.idle_s = pool, log, idle_s
        self.timing: dict[str, dict] | None = {} if timing else None
        self.sem = asyncio.Semaphore(max(1, int(concurrency)))
        self.ended = 0
        self.outcomes: collections.Counter = collections.Counter()
        self.experts: dict[str, Expert] = {}
        self.stopping: set[asyncio.Task] = set()
        self.done: dict | None = None

    async def connection(self, ws, on_message=None) -> str:
        out: asyncio.Queue = asyncio.Queue()
        scenes: dict[str, tuple[asyncio.Queue, asyncio.Task]] = {}

        async def writer() -> None:
            while True:
                m = await out.get()
                await ws.send(json.dumps(m, separators=(",", ":")))

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
                t, key = m.get("type"), m.get("scene_key")
                if isinstance(key, str) and t == "start":
                    if key not in scenes or scenes[key][1].done():
                        q: asyncio.Queue = asyncio.Queue()
                        scenes[key] = (q, asyncio.ensure_future(self.scene(key, m, q, out, time.monotonic())))
                elif isinstance(key, str) and t in ("state", "end"):
                    if key in scenes and not scenes[key][1].done():
                        scenes[key][0].put_nowait(m)
                elif t == "done":
                    self.done = m
                    return "done"
                elif t == "welcome":
                    self.log(_welcome(m))
                elif t == "error" and key is None:
                    self.log(f"the server says: {_says(m)}")
        finally:
            w.cancel()
            tasks = [t for _, t in scenes.values() if not t.done()]
            for t in tasks:
                t.cancel()
            for t in tasks:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            try:
                await w
            except (asyncio.CancelledError, Exception):
                pass

    async def scene(self, key: str, start: dict, inbox: asyncio.Queue, out: asyncio.Queue,
                    got_at: float | None = None) -> None:
        clock = {"sent": got_at if got_at is not None else time.monotonic()}
        acts: list[float] = []
        if self.timing is not None:
            self.timing[key] = {"scene_id": (start.get("scene") or {}).get("scene_id"), "acts": acts}
        async with self.sem:
            t = asyncio.ensure_future(self.pool.take())
            try:
                ex = await asyncio.shield(t)
            except asyncio.CancelledError:
                try:
                    await (await t).stop(0)
                except Exception:
                    pass
                raise
            self.experts[key] = ex
            self.pool.launcher.label(ex, key, start)
            state = {"ended": False}

            async def pump() -> None:
                try:
                    while True:
                        line = await ex.readline()
                        if line is None:
                            break
                        if not line.strip():
                            continue
                        if clock["sent"] is not None:
                            acts.append(time.monotonic() - clock["sent"])
                            clock["sent"] = None
                        try:
                            m = json.loads(line)
                        except (ValueError, RecursionError):
                            m = None
                        if not isinstance(m, dict):
                            out.put_nowait({"type": "error", "scene_key": key,
                                            "error": "the expert wrote a line that is not a JSON object"})
                            return
                        m["scene_key"] = key
                        out.put_nowait(m)
                except ServeError as e:
                    out.put_nowait({"type": "error", "scene_key": key, "error": str(e)})
                    return
                if not state["ended"]:
                    rc = await ex.proc.wait()
                    if not state["ended"]:
                        out.put_nowait({"type": "error", "scene_key": key,
                                        "error": f"the expert exited (code {rc}) before the scene ended"})

            p = asyncio.ensure_future(pump())
            try:
                await ex.write(_plain(start))
                while True:
                    try:
                        m = await asyncio.wait_for(inbox.get(), self.idle_s)
                    except asyncio.TimeoutError:
                        self.log(f"scene {key}: no message from the server for {self.idle_s:g} s; stopping it")
                        break
                    if m.get("type") == "end":
                        state["ended"] = True
                        if self.timing is not None and clock["sent"] is not None:
                            self.timing[key]["pending"] = time.monotonic() - clock["sent"]
                        await ex.write(_plain(m))
                        self.ended += 1
                        self.outcomes[str(m.get("outcome"))] += 1
                        self.log(f"scene {key}: {m.get('outcome')} t={m.get('t')}"
                                 + (f" ({m['error']})" if m.get("error") else "") + f"  [{self.ended} ended]")
                        break
                    clock["sent"] = time.monotonic()
                    await ex.write(_plain(m))
            finally:
                ok = state["ended"]
                state["ended"] = True
                p.cancel()
                try:
                    await p
                except (asyncio.CancelledError, Exception):
                    pass
                self.experts.pop(key, None)
                st = asyncio.ensure_future(ex.stop(END_GRACE_S if ok else 0))
                self.stopping.add(st)
                st.add_done_callback(self.stopping.discard)
                await asyncio.shield(st)

    async def drain(self) -> None:
        while self.stopping:
            await asyncio.gather(*list(self.stopping), return_exceptions=True)


def _welcome(m: dict) -> str:
    line = "connected"
    if m.get("submission") is not None:
        line += f": submission {m['submission']}"
    if m.get("status") is not None:
        line += f", status {m['status']}"
    total = m.get("scenes", m.get("total"))
    if m.get("done") is not None or total is not None:
        line += f", {m.get('done') if m.get('done') is not None else '?'}/{total if total is not None else '?'} scenes done"
    first, step = _num(m.get("first_step_deadline_s")), _num(m.get("step_deadline_s"))
    if first is not None or step is not None:
        line += "; deadlines:" + (f" first act {first:g} s" if first is not None else "") + \
                (("," if first is not None else "") + f" each later act {step:g} s" if step is not None else "")
    return line


def _done(m: dict) -> str:
    line = "the server says phase 1 is finished"
    if m.get("status") is not None:
        line += f": status {m['status']}"
    if m.get("score") is not None:
        line += f", score {m['score']}"
        if m.get("successes") is not None and m.get("scenes") is not None:
            line += f" ({m['successes']}/{m['scenes']})"
    if m.get("detail"):
        line += f" ({m['detail']})"
    return line


async def _sleep_or_stop(stop: asyncio.Event, s: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), s)
    except asyncio.TimeoutError:
        pass


async def serve(api: str, image: str, keypair, launcher: Launcher, *, concurrency: int = 8, warm: int | None = None,
                stop: asyncio.Event | None = None, backoff: tuple[float, float] = BACKOFF, log=_log,
                max_attempts: int | None = None, idle_s: float = IDLE_S) -> int:
    url = ws_url(api)
    stop = stop or asyncio.Event()
    pool = Pool(launcher, concurrency if warm is None else warm)
    bridge = Bridge(pool, concurrency, log, idle_s)
    delay, attempts, nonce_refusals = backoff[0], 0, 0
    try:
        await pool.fill()
        while not stop.is_set():
            attempts += 1
            accepted = {"ok": False, "error": None}

            def seen(m: dict) -> None:
                if m.get("type") == "welcome":
                    accepted["ok"] = True
                elif m.get("type") == "error" and m.get("scene_key") is None:
                    accepted["error"] = m

            why, wait = None, None
            try:
                nonce = await asyncio.to_thread(Api(api, timeout=OPEN_TIMEOUT_S).nonce)
                async with ws_connect(url, max_size=MAX_MESSAGE, open_timeout=OPEN_TIMEOUT_S, ping_interval=20,
                                      ping_timeout=20, close_timeout=5) as ws:
                    await ws.send(json.dumps(hello(keypair, image, nonce), separators=(",", ":")))
                    run = asyncio.ensure_future(bridge.connection(ws, seen))
                    halt = asyncio.ensure_future(stop.wait())
                    done, _ = await asyncio.wait({run, halt}, return_when=asyncio.FIRST_COMPLETED)
                    if run in done:
                        halt.cancel()
                        if run.result() == "done":
                            await ws.close(1000, "phase 1 finished")
                            m = bridge.done or {}
                            log(_done(m))
                            r = refusal(4403, "", {"reason": "not_open", "retry_at": m.get("retry_at")}) \
                                if m.get("status") == "review" else None
                            if r is None or r["final"]:
                                log(f"done: {bridge.ended} scenes ended here ({_counts(bridge)})")
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
            log(f"{why}; reconnecting in {wait:.1f} s (unfinished scenes resume on the server)")
            await _sleep_or_stop(stop, wait)
            delay = min(backoff[1], delay * 2)
        log(f"stopped: {bridge.ended} scenes ended here ({_counts(bridge)})")
        return 0
    finally:
        await bridge.drain()
        await pool.close()


def _counts(b: Bridge) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(b.outcomes.items())) or "none"


def run(api: str, image: str, keypair, launcher: Launcher, **kw) -> int:
    async def main() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(s, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        return await serve(api, image, keypair, launcher, stop=stop, **kw)

    return asyncio.run(main())
