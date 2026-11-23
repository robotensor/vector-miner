from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from http import HTTPStatus
from pathlib import Path

import pytest
from bittensor_wallet import Keypair
from websockets.asyncio.server import serve as ws_serve
from websockets.exceptions import ConnectionClosed

from vector_miner import serve as sv
from vector_miner.protocol import connect_message

HERE = Path(__file__).resolve().parent
REF = "ghcr.io/alice/bin-expert@sha256:" + "a" * 64
KP = Keypair.create_from_uri("//Alice")
ARM = [0, 1, 2, 3, 4, 5]
FAST = (0.01, 0.05)


def expert(mode="track", per=5):
    return {"command": [sys.executable, str(HERE / "fake_expert.py"), mode, str(per)]}


class FakeServer:
    def __init__(self, script, refuse=None):
        self.script, self.refuse = script, refuse
        self.nonces, self.hellos, self.closes = set(), [], []
        self.conns = 0

    async def process_request(self, connection, request):
        if request.path.startswith("/v1/expert/nonce"):
            n = uuid.uuid4().hex
            self.nonces.add(n)
            return connection.respond(HTTPStatus.OK, json.dumps({"nonce": n}))
        if request.path != sv.CONNECT:
            return connection.respond(HTTPStatus.NOT_FOUND, "{}")
        return None

    async def handler(self, ws):
        h = json.loads(await asyncio.wait_for(ws.recv(), 10))
        self.hellos.append(h)
        ok = (h.get("type") == "hello" and h.get("nonce") in self.nonces
              and Keypair(ss58_address=h["hotkey"]).verify(connect_message(h["image"], h["nonce"]),
                                                          bytes.fromhex(h["signature"])))
        self.nonces.discard(h.get("nonce"))
        if not ok:
            await ws.close(1008, "bad hello")
            return
        if self.refuse:
            await ws.close(*self.refuse)
            return
        self.conns += 1
        try:
            await self.script(ws, self)
        except ConnectionClosed:
            pass
        self.closes.append(ws.close_code)

    async def __aenter__(self):
        self.srv = await ws_serve(self.handler, "127.0.0.1", 0, process_request=self.process_request).__aenter__()
        self.api = f"http://127.0.0.1:{self.srv.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *exc):
        self.srv.close()
        await self.srv.wait_closed()


def start_msg(i: int, key: str) -> dict:
    return {"type": "start", "format": "vector-expert/2", "scene_key": key,
            "scene": {"scene_id": f"s{i}", "xml": "<mujoco/>", "meta": {"robot": {"arm_joints_qpos": ARM}}},
            "tick": 0, "t": 0.0, "qpos": [0.1 * i] * 6, "qvel": [0.0] * 6, "obs": {"width": 1}, "limits": {}}


class Router:
    def __init__(self, ws):
        self.ws, self.boxes = ws, {}
        self.task = asyncio.ensure_future(self.read())

    def box(self, key):
        return self.boxes.setdefault(key, asyncio.Queue())

    async def read(self):
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                self.box(m.get("scene_key")).put_nowait(m)
        except ConnectionClosed:
            pass

    async def play(self, i, key, acts=3, got=None):
        await self.ws.send(json.dumps(start_msg(i, key)))
        rows = []
        for k in range(acts):
            m = await asyncio.wait_for(self.box(key).get(), 10)
            if m["type"] != "act":
                if got is not None:
                    got[key] = m
                return m
            rows += m["rows"]
            if k == acts - 1:
                break
            await self.ws.send(json.dumps({"type": "state", "scene_key": key, "tick": (k + 1) * 5, "t": 0.25 * (k + 1),
                                           "qpos": [0.1 * i] * 6, "qvel": [0.0] * 6}))
        await self.ws.send(json.dumps({"type": "end", "scene_key": key, "outcome": "missed", "t": 1.0, "tick": 15}))
        if got is not None:
            got[key] = rows
        return rows


def run(coro, timeout=60):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def pids_of(d: Path) -> list[int]:
    return sorted(int(p.name) for p in d.iterdir() if p.name.isdigit())


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


@pytest.fixture
def pids(tmp_path, monkeypatch):
    d = tmp_path / "pids"
    d.mkdir()
    monkeypatch.setenv("FAKE_EXPERT_PIDS", str(d))
    return d


def test_ws_url_and_close_codes():
    assert sv.ws_url("https://vector.example/") == "wss://vector.example/v1/expert/connect"
    assert sv.ws_url("http://127.0.0.1:9") == "ws://127.0.0.1:9/v1/expert/connect"
    with pytest.raises(sv.ServeError):
        sv.ws_url("vector.example")
    assert sv.fatal_close(1008) and sv.fatal_close(4003) and not sv.fatal_close(1011) and not sv.fatal_close(1000)
    assert not sv.fatal_close(None)


def test_hello_is_signed_and_scenes_are_bridged_by_key(pids):
    got = {}

    async def script(ws, srv):
        r = Router(ws)
        await ws.send(json.dumps({"type": "welcome", "submission": "sub-1", "done": 0, "total": 3}))
        await asyncio.gather(*(r.play(i, f"{i}.0", got=got) for i in range(3)))
        await ws.send(json.dumps({"type": "done"}))
        await asyncio.sleep(0.2)

    async def main():
        async with FakeServer(script) as srv:
            code = await sv.serve(srv.api, REF, KP, sv.Launcher(expert()), concurrency=3, warm=1, backoff=FAST)
            return code, srv

    code, srv = run(main())
    assert code == 0
    h = srv.hellos[0]
    assert set(h) == {"type", "hotkey", "image", "nonce", "signature"} and h["hotkey"] == KP.ss58_address
    assert h["image"] == REF and KP.verify(f"vector-connect\n{REF}\n{h['nonce']}".encode(), bytes.fromhex(h["signature"]))
    assert sorted(got) == ["0.0", "1.0", "2.0"]
    for i in range(3):
        rows = got[f"{i}.0"]
        assert len(rows) == 15 and all(len(x) == 7 for x in rows), rows
        assert rows[0][0] == pytest.approx(0.1 * i - 0.009)
    ends = sorted(p for p in pids.iterdir() if p.name.endswith(".end"))
    assert len(ends) == 3 and all(json.loads(p.read_text())["outcome"] == "missed" for p in ends)
    assert all("scene_key" not in json.loads(p.read_text()) for p in ends)
    used = {int(p.name.split(".")[0]) for p in ends}
    assert len(used) == 3, "every scene needs its own process"
    assert not any(alive(p) for p in pids_of(pids)), "every process (warm ones too) must be gone"


def test_concurrency_and_warm_pool_give_a_fresh_process_per_scene(pids):
    seen = {"max": 0}

    async def script(ws, srv):
        r = Router(ws)
        await asyncio.gather(*(r.play(i, f"{i}.0") for i in range(5)))
        await ws.send(json.dumps({"type": "done"}))

    async def main():
        async with FakeServer(script) as srv:
            launcher = sv.Launcher(expert())
            task = asyncio.ensure_future(sv.serve(srv.api, REF, KP, launcher, concurrency=2, warm=2, backoff=FAST))
            while not task.done():
                n = sum(1 for p in pids_of(pids) if alive(p) and not (pids / f"{p}.end").exists())
                seen["max"] = max(seen["max"], n)
                await asyncio.sleep(0.005)
            return task.result()

    assert run(main()) == 0
    ends = [p for p in pids.iterdir() if p.name.endswith(".end")]
    assert len(ends) == 5 and len({p.name for p in ends}) == 5
    assert seen["max"] <= 4, seen
    assert not any(alive(p) for p in pids_of(pids))


def test_expert_failures_are_reported_to_the_server(pids):
    got = {}

    async def script(ws, srv):
        r = Router(ws)
        await r.play(0, "0.0", got=got)
        await ws.send(json.dumps({"type": "end", "scene_key": "0.0", "outcome": "expert_error", "t": 0, "tick": 0}))
        await ws.send(json.dumps({"type": "done"}))

    async def main(mode):
        async with FakeServer(script) as srv:
            return await sv.serve(srv.api, REF, KP, sv.Launcher(expert(mode)), concurrency=1, warm=0, backoff=FAST)

    assert run(main("crash")) == 0
    assert got["0.0"]["type"] == "error" and "exited (code 3)" in got["0.0"]["error"], got
    assert run(main("junk")) == 0
    assert got["0.0"]["type"] == "error" and "not a JSON object" in got["0.0"]["error"], got


def test_stray_keys_and_unknown_messages_are_ignored(pids):
    got = {}

    async def script(ws, srv):
        r = Router(ws)
        await ws.send("not json")
        await ws.send(json.dumps([1, 2]))
        await ws.send(json.dumps({"type": "state", "scene_key": "9.9", "tick": 5}))
        await ws.send(json.dumps({"type": "mystery"}))
        await ws.send(json.dumps({"type": "error", "error": "just saying"}))
        await r.play(0, "0.0", got=got)
        await ws.send(json.dumps({"type": "done"}))

    async def main():
        async with FakeServer(script) as srv:
            return await sv.serve(srv.api, REF, KP, sv.Launcher(expert()), concurrency=1, warm=0, backoff=FAST)

    assert run(main()) == 0 and len(got["0.0"]) == 15


def test_reconnect_with_backoff_kills_running_scenes_and_resumes(pids):
    got, logs = {}, []

    async def script(ws, srv):
        r = Router(ws)
        if srv.conns == 1:
            await ws.send(json.dumps(start_msg(0, "0.0")))
            await asyncio.wait_for(r.box("0.0").get(), 10)
            await ws.close(1011, "server restart")
            return
        await r.play(0, "0.1", got=got)
        await ws.send(json.dumps({"type": "done"}))

    async def main():
        async with FakeServer(script) as srv:
            code = await sv.serve(srv.api, REF, KP, sv.Launcher(expert()), concurrency=1, warm=0, backoff=FAST,
                                  log=logs.append)
            return code, srv

    code, srv = run(main())
    assert code == 0 and srv.conns == 2 and len(srv.hellos) == 2
    assert srv.hellos[0]["nonce"] != srv.hellos[1]["nonce"], "every connection signs a fresh nonce"
    assert len(got["0.1"]) == 15
    assert any("reconnecting in" in x and "1011" in x for x in logs), logs
    ps = pids_of(pids)
    assert len(ps) == 2 and not any(alive(p) for p in ps)
    assert len([p for p in pids.iterdir() if p.name.endswith(".end")]) == 1


def test_a_refused_hello_stops_without_retrying():
    async def main(refuse):
        async with FakeServer(None, refuse=refuse) as srv:
            code = await sv.serve(srv.api, REF, KP, sv.Launcher(expert()), concurrency=1, warm=0, backoff=FAST,
                                  log=logs.append)
            return code, srv

    logs = []
    code, srv = run(main((4003, "no submission for this hotkey and image")))
    assert code == 1 and len(srv.hellos) == 1
    assert any("refused" in x and "no submission for this hotkey" in x for x in logs), logs


def test_an_unreachable_server_is_retried_then_given_up(tmp_path):
    logs = []

    async def main():
        return await sv.serve("http://127.0.0.1:9", REF, KP, sv.Launcher(expert()), concurrency=1, warm=0,
                              backoff=FAST, max_attempts=3, log=logs.append)

    assert run(main()) == 2
    assert sum("reconnecting in" in x for x in logs) == 2 and "giving up after 3 attempts" in logs[-1], logs


def test_stop_closes_the_socket_and_kills_every_process(pids):
    async def script(ws, srv):
        r = Router(ws)
        await ws.send(json.dumps(start_msg(0, "0.0")))
        await asyncio.wait_for(r.box("0.0").get(), 10)
        await ws.wait_closed()

    async def main():
        async with FakeServer(script) as srv:
            stop = asyncio.Event()
            task = asyncio.ensure_future(sv.serve(srv.api, REF, KP, sv.Launcher(expert()), concurrency=2, warm=2,
                                                  backoff=FAST, stop=stop))
            while srv.conns == 0 or len(pids_of(pids)) < 3:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.2)
            stop.set()
            code = await task
            await asyncio.sleep(0.1)
            return code, srv

    code, srv = run(main())
    assert code == 0 and srv.closes == [1000]
    assert len(pids_of(pids)) >= 3 and not any(alive(p) for p in pids_of(pids))


def test_sigterm_shuts_the_cli_down_cleanly(pids, tmp_path):
    ready = tmp_path / "server.json"
    srv_code = f'''
import asyncio, json, sys
sys.path.insert(0, {str(HERE)!r})
from test_serve import FakeServer, Router, start_msg

async def script(ws, srv):
    r = Router(ws)
    await ws.send(json.dumps(start_msg(0, "0.0")))
    await r.box("0.0").get()
    open({str(tmp_path / "acted")!r}, "w").close()
    await ws.wait_closed()

async def main():
    async with FakeServer(script) as srv:
        open({str(ready)!r}, "w").write(json.dumps({{"api": srv.api}}))
        while not srv.closes:
            await asyncio.sleep(0.05)
        open({str(tmp_path / "closes")!r}, "w").write(json.dumps(srv.closes))

asyncio.run(main())
'''
    server = subprocess.Popen([sys.executable, "-c", srv_code])
    try:
        t0 = time.time()
        while not ready.exists():
            assert time.time() - t0 < 20 and server.poll() is None
            time.sleep(0.05)
        api = json.loads(ready.read_text())["api"]
        miner = f'''
import sys
from bittensor_wallet import Keypair
from vector_miner import serve as sv
cmd = [sys.executable, {str(HERE / "fake_expert.py")!r}, "track", "5"]
sys.exit(sv.run({api!r}, {REF!r}, Keypair.create_from_uri("//Alice"), sv.Launcher({{"command": cmd}}), concurrency=1,
                warm=1))
'''
        p = subprocess.Popen([sys.executable, "-c", miner], stderr=subprocess.PIPE, text=True)
        while not (tmp_path / "acted").exists():
            assert time.time() - t0 < 30 and p.poll() is None
            time.sleep(0.05)
        p.send_signal(signal.SIGTERM)
        _, err = p.communicate(timeout=30)
        assert p.returncode == 0, err
        assert "stopped: 0 scenes ended here" in err
        server.wait(timeout=20)
        assert json.loads((tmp_path / "closes").read_text()) == [1000]
        assert len(pids_of(pids)) == 2 and not any(alive(x) for x in pids_of(pids))
    finally:
        server.kill()


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "docker.log"
    b = tmp_path / "docker"
    b.write_text(f"#!/bin/sh\nFAKE_DOCKER_LOG={log} exec {sys.executable} {HERE / 'fake_docker.py'} \"$@\"\n")
    b.chmod(0o755)
    return b, log


def test_image_runs_with_vector_envs_hardened_docker_flags(pids, tmp_path):
    harness = pytest.importorskip("vector_env.harness")
    b, log = _fake_docker(tmp_path)
    docker = {"binary": str(b)}
    lz = sv.Launcher({"image": REF}, docker, Path("/b"))
    sp = lz.spec()
    want, kill = harness.docker_argv(REF, sp["name"], docker, Path("/b"))
    assert sp["argv"] == want and sp["kill"] == kill == [str(b), "kill", sp["name"]]
    flat = " ".join(sp["argv"])
    for flag in ("run --rm -i --pull never --network none", "--read-only", "--cap-drop ALL", "no-new-privileges",
                 "--memory-swap", "--pids-limit", "-v /b:/bundle:ro"):
        assert flag in flat, flag
    assert sp["argv"][-1] == REF
    got = {}

    async def script(ws, srv):
        r = Router(ws)
        await r.play(0, "0.0", got=got)
        await ws.send(json.dumps(start_msg(1, "1.0")))
        await asyncio.wait_for(r.box("1.0").get(), 10)
        await ws.send(json.dumps({"type": "end", "scene_key": "1.0", "outcome": "step_timeout", "t": 0, "tick": 0}))
        await asyncio.sleep(0.3)
        await ws.send(json.dumps({"type": "done"}))

    async def main():
        async with FakeServer(script) as srv:
            return await sv.serve(srv.api, REF, KP, sv.Launcher({"image": REF}, docker, None), concurrency=1, warm=1,
                                  backoff=FAST)

    assert run(main()) == 0
    assert len(got["0.0"]) == 9
    calls = [json.loads(x) for x in log.read_text().splitlines()]
    runs = [c for c in calls if c[0] == "run"]
    assert len(runs) >= 2 and all("--network" in c and c[c.index("--network") + 1] == "none" for c in runs)
    assert len({c[c.index("--name") + 1] for c in runs}) == len(runs), "one container per scene"


def test_cli_serve_checks_its_arguments(monkeypatch, capsys):
    from vector_miner import cli
    monkeypatch.delenv("VECTOR_MINER_API", raising=False)
    with pytest.raises(SystemExit, match="serve the image you committed"):
        cli.main(["serve", "--image", "ghcr.io/a/b:latest", "--api", "http://x"])
    with pytest.raises(SystemExit, match="--api"):
        cli.main(["serve", "--image", REF, "--command", "true"])
    with pytest.raises(SystemExit, match="--concurrency"):
        cli.main(["serve", "--image", REF, "--command", "true", "--api", "http://x", "--concurrency", "0"])
    monkeypatch.setattr(cli, "_keypair", lambda a: KP)
    seen = {}
    monkeypatch.setattr(sv, "run", lambda api, ref, kp, launcher, **k: seen.update(api=api, ref=ref, k=k,
                                                                                    ex=launcher.expert) or 0)
    assert cli.main(["serve", "--image", REF, "--command", "node solve.mjs", "--api", "http://x", "--concurrency", "3"]) == 0
    assert seen == {"api": "http://x", "ref": REF, "k": {"concurrency": 3, "warm": None},
                    "ex": {"command": ["node", "solve.mjs"]}}


def test_cli_serve_image_needs_it_locally(monkeypatch, capsys, tmp_path):
    pytest.importorskip("vector_env.harness")
    from vector_miner import cli
    monkeypatch.setattr(cli, "_keypair", lambda a: KP)
    assert cli.main(["serve", "--image", REF, "--api", "http://x", "--docker", str(tmp_path / "none")]) == 2
    assert "cannot run" in capsys.readouterr().err


class WsChannel:
    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()
        self.ws = None
        self.n = 0
        self.attached = asyncio.Event()

    async def attach(self, ws):
        if self.ws is not None:
            from vector_env.harness import Reconnected
            self.q.put_nowait(Reconnected())
        self.ws = ws
        self.attached.set()
        try:
            async for raw in ws:
                self.n += 1
                self.q.put_nowait(json.loads(raw))
        except ConnectionClosed:
            pass
        self.attached.clear()

    async def send(self, msg):
        while True:
            await asyncio.wait_for(self.attached.wait(), 30)
            try:
                return await self.ws.send(json.dumps(msg))
            except ConnectionClosed:
                self.attached.clear()

    async def recv(self):
        m = await self.q.get()
        if isinstance(m, Exception):
            raise m
        return m


@pytest.fixture(scope="module")
def scenes2():
    assets = pytest.importorskip("vector_env.assets")
    if assets.find() is None:
        pytest.skip(assets.MISSING)
    from vector_env import scenes
    return [scenes.generate(s) for s in (1, 2)]


@pytest.mark.parametrize("drop", [False, True])
def test_the_real_harness_over_the_socket_matches_a_local_run(scenes2, drop):
    from vector_env import harness
    res = {}
    T, per = (15, 1) if drop else (3, 5)

    async def main():
        ch = WsChannel()

        async def script(ws, srv):
            if srv.conns == 1:
                res["run"] = asyncio.ensure_future(harness.arun(
                    {"remote": ch}, scenes2, time_limit_s=T, wall_s=120, workers=2))
            if drop and srv.conns == 1:
                t = asyncio.ensure_future(ch.attach(ws))
                while ch.n < 4:
                    await asyncio.sleep(0.005)
                await ws.close(1011, "server restart")
                await t
                return
            t = asyncio.ensure_future(ch.attach(ws))
            out = await res["run"]
            res["out"] = out
            await ws.send(json.dumps({"type": "done"}))
            await t

        async with FakeServer(script) as srv:
            return await sv.serve(srv.api, REF, KP, sv.Launcher(expert("track", per)), concurrency=2, warm=2,
                                  backoff=FAST)

    assert run(main(), 300) == 0
    remote = res["out"]
    local = harness.run(expert("track", per), scenes2, time_limit_s=T, wall_s=120, workers=2)
    c = harness.compare(remote, local)
    assert c["all_equal"] and c["compared"] == 2, c
    assert all(r["outcome"] not in ("disconnected", "harness_error", "step_timeout") for r in remote), remote
    if drop:
        assert any(r["restarts"] >= 1 for r in remote), remote
