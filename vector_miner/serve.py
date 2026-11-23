from __future__ import annotations

import asyncio
import collections
import json
import os
import random
import shlex
import signal
import subprocess
import sys
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


def fatal_close(code: int | None) -> bool:
    return code is not None and (code == 1008 or 4000 <= code <= 4999)


class Launcher:
    def __init__(self, expert: dict, docker: dict | None = None, bundle: Path | None = None,
                 stderr_dir: str | Path | None = None):
        if not expert.get("command") and not expert.get("image"):
            raise ServeError("the expert needs an image or a command")
        self.expert, self.docker, self.bundle = expert, docker, bundle
        self.stderr_dir = Path(stderr_dir) if stderr_dir else None
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
            await self.ready.popleft().stop(0)


def _plain(msg: dict) -> dict:
    return {k: v for k, v in msg.items() if k != "scene_key"}


class Bridge:
    def __init__(self, pool: Pool, concurrency: int, log=_log, idle_s: float = IDLE_S):
        self.pool, self.log, self.idle_s = pool, log, idle_s
        self.sem = asyncio.Semaphore(max(1, int(concurrency)))
        self.ended = 0
        self.outcomes: collections.Counter = collections.Counter()
        self.experts: dict[str, Expert] = {}
        self.stopping: set[asyncio.Task] = set()

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
                        scenes[key] = (q, asyncio.ensure_future(self.scene(key, m, q, out)))
                elif isinstance(key, str) and t in ("state", "end"):
                    if key in scenes and not scenes[key][1].done():
                        scenes[key][0].put_nowait(m)
                elif t == "done":
                    self.log(f"the server says phase 1 is finished{': ' + str(m['detail']) if m.get('detail') else ''}")
                    return "done"
                elif t == "welcome":
                    self.log("connected" + "".join(f", {k} {m[k]}" for k in ("submission", "done", "total", "resumed")
                                                   if m.get(k) is not None))
                elif t == "error" and key is None:
                    self.log(f"the server says: {m.get('error')}")
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

    async def scene(self, key: str, start: dict, inbox: asyncio.Queue, out: asyncio.Queue) -> None:
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
            state = {"ended": False}

            async def pump() -> None:
                try:
                    while True:
                        line = await ex.readline()
                        if line is None:
                            break
                        if not line.strip():
                            continue
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
                        await ex.write(_plain(m))
                        self.ended += 1
                        self.outcomes[str(m.get("outcome"))] += 1
                        self.log(f"scene {key}: {m.get('outcome')} t={m.get('t')}"
                                 + (f" ({m['error']})" if m.get("error") else "") + f"  [{self.ended} ended]")
                        break
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
    delay, attempts = backoff[0], 0
    try:
        await pool.fill()
        while not stop.is_set():
            attempts += 1
            accepted = {"ok": False}

            def seen(m: dict) -> None:
                accepted["ok"] = True

            why = None
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
                            log(f"done: {bridge.ended} scenes ended here ({_counts(bridge)})")
                            return 0
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
                if fatal_close(code):
                    log(f"the server refused this miner (close {code}): {reason or 'no reason given'}")
                    return 1
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
