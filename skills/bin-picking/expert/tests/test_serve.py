from __future__ import annotations

import asyncio
import datetime as dt
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

from vector_miner.protocol import connect_message
from vector_miner.skills.bin_picking.expert import serve as sv

HERE = Path(__file__).resolve().parent
REF = "ghcr.io/alice/bin-expert@sha256:" + "a" * 64
KP = Keypair.create_from_uri("//Alice")
ARM = [0, 1, 2, 3, 4, 5]
FAST = (0.01, 0.05)


def expert(mode="track", per=5):
    return {"command": [sys.executable, str(HERE / "fake_expert.py"), mode, str(per)]}


def runner(mode="ok", secs=0.05, **kw):
    """A Runner whose scene process is tests/fake_play.py (a claim at once) instead of vector-env's harness."""
    return sv.Runner({"command": ["fake", mode, str(secs)]}, argv=[sys.executable, str(HERE / "fake_play.py")], **kw)


class FakeServer:
    def __init__(self, script, refuse=None, refusals=None):
        self.script, self.refuse, self.refusals = script, refuse, list(refusals or [])
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
        if self.refusals:
            err, code, reason = self.refusals.pop(0)
            if err is not None:
                await ws.send(json.dumps({"type": "error", **err}))
            await ws.close(code, reason)
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


LIMITS = {"time_limit_s": 1.0, "picks": 10, "max_rows": 20, "tick_s": 0.05, "control_hz": 20, "wall_s": 120.0}


def scene_msg(i: int, sid: str | None = None) -> dict:
    return {"type": "scene", "index": i, "scene": {"scene_id": sid or f"s{i}", "xml": "<mujoco/>", "meta": {}},
            "limits": LIMITS}


class Inbox:
    def __init__(self, ws):
        self.ws, self.results, self.other = ws, asyncio.Queue(), []
        self.task = asyncio.ensure_future(self.read())

    async def read(self):
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                (self.results.put_nowait if m.get("type") == "result" else self.other.append)(m)
        except ConnectionClosed:
            pass

    async def result(self, timeout=20):
        return await asyncio.wait_for(self.results.get(), timeout)


async def answer(ws, box, i, ack=True):
    await ws.send(json.dumps(scene_msg(i)))
    m = await box.result()
    if ack:
        await ws.send(json.dumps({"type": "ack", "index": m["index"], "ok": True, "reason": None,
                                  "score": m["result"]["score"]}))
    return m


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
    assert not sv.fatal_close(4429, "busy: every phase-1 slot is in use")
    busy = sv.refusal(4429, "busy: try again", {"type": "error", "reason": "busy", "message": "m", "retry_after_s": 120})
    assert busy["wait_s"] == 120 and not busy["final"] and busy["message"] == "m"
    assert sv.refusal(4429, "busy: try again")["wait_s"] == sv.BUSY_WAIT_S
    now = sv._when("2026-11-23T10:00:00+00:00")
    later = sv.refusal(4403, "not_open: phase 1 hit failures", {"reason": "not_open", "retry_at": "2026-11-23T16:00:00Z"},
                       now=now)
    assert not later["final"] and later["wait_s"] == 6 * 3600 and later["retry_at"] == "2026-11-23T16:00:00Z"
    over = sv.refusal(4403, "not_open: phase 1 of submission s is over (status ranked)")
    assert over["final"] and over["reason"] == "not_open" and over["message"].startswith("phase 1 of submission")
    for code, reason in ((4403, "image: hello.image is not the committed image (x)"),
                         (4404, "unknown: no submission for this hotkey"),
                         (4401, "bad_hello: the signature does not verify (sr25519 over ...)"),
                         (4409, "replaced: a newer connection of this hotkey took over"),
                         (4403, "duplicate_hotkey: this hotkey already has a submission")):
        assert sv.fatal_close(code, reason), reason
    nonce = sv.refusal(4401, "bad_hello: unknown or expired nonce (GET /v1/expert/nonce)")
    assert not nonce["final"] and nonce["nonce"]
    idle = sv.refusal(4403, "not_running: the competition is planned",
                      {"reason": "not_running", "message": "the competition is planned",
                       "retry_at": "2026-11-23T12:00:00+00:00"}, now=now)
    assert not idle["final"] and idle["wait_s"] == 2 * 3600 and idle["retry_at"] == "2026-11-23T12:00:00+00:00"
    gone = sv.refusal(4403, "not_running: the competition is finished",
                      {"reason": "not_running", "message": "the competition is finished"})
    assert gone["final"] and gone["reason"] == "not_running" and gone["message"] == "the competition is finished"
    assert sv.fatal_close(4403, "not_running: the competition is paused")
    assert not sv.fatal_close(4408, "timeout: send the hello within 10 s")


def test_ws_url_and_close_codes():
    assert sv.ws_url("https://vector.example/") == "wss://vector.example/v1/expert/connect"
    assert sv.ws_url("http://127.0.0.1:9") == "ws://127.0.0.1:9/v1/expert/connect"
    with pytest.raises(sv.ServeError):
        sv.ws_url("vector.example")
    assert sv.fatal_close(1008) and sv.fatal_close(4003) and not sv.fatal_close(1011) and not sv.fatal_close(1000)
    assert not sv.fatal_close(None)
    assert not sv.fatal_close(4429, "busy: every phase-1 slot is in use")
    busy = sv.refusal(4429, "busy: try again", {"type": "error", "reason": "busy", "message": "m", "retry_after_s": 120})
    assert busy["wait_s"] == 120 and not busy["final"] and busy["message"] == "m"
    assert sv.refusal(4429, "busy: try again")["wait_s"] == sv.BUSY_WAIT_S
    now = sv._when("2026-11-23T10:00:00+00:00")
    later = sv.refusal(4403, "not_open: phase 1 hit failures", {"reason": "not_open", "retry_at": "2026-11-23T16:00:00Z"},
                       now=now)
    assert not later["final"] and later["wait_s"] == 6 * 3600 and later["retry_at"] == "2026-11-23T16:00:00Z"
    over = sv.refusal(4403, "not_open: phase 1 of submission s is over (status ranked)")
    assert over["final"] and over["reason"] == "not_open" and over["message"].startswith("phase 1 of submission")
    for code, reason in ((4403, "image: hello.image is not the committed image (x)"),
                         (4404, "unknown: no submission for this hotkey"),
                         (4401, "bad_hello: the signature does not verify (sr25519 over ...)"),
                         (4409, "replaced: a newer connection of this hotkey took over"),
                         (4403, "duplicate_hotkey: this hotkey already has a submission")):
        assert sv.fatal_close(code, reason), reason
    nonce = sv.refusal(4401, "bad_hello: unknown or expired nonce (GET /v1/expert/nonce)")
    assert not nonce["final"] and nonce["nonce"]
    idle = sv.refusal(4403, "not_running: the competition is planned",
                      {"reason": "not_running", "message": "the competition is planned",
                       "retry_at": "2026-11-23T12:00:00+00:00"}, now=now)
    assert not idle["final"] and idle["wait_s"] == 2 * 3600 and idle["retry_at"] == "2026-11-23T12:00:00+00:00"
    gone = sv.refusal(4403, "not_running: the competition is finished",
                      {"reason": "not_running", "message": "the competition is finished"})
    assert gone["final"] and gone["reason"] == "not_running" and gone["message"] == "the competition is finished"
    assert sv.fatal_close(4403, "not_running: the competition is paused")
    assert not sv.fatal_close(4408, "timeout: send the hello within 10 s")


def test_hello_is_signed_and_scenes_run_here_one_result_each(pids):
    got, logs = [], []

    async def script(ws, srv):
        box = Inbox(ws)
        await ws.send(json.dumps({"type": "welcome", "submission": "sub-1", "status": "phase1", "done": 0, "scenes": 3,
                                  "scenes_at_once": 2, "scene_deadline_s": 1800}))
        await ws.send(json.dumps(scene_msg(0)))
        await ws.send(json.dumps(scene_msg(1)))
        for _ in range(2):
            got.append(await box.result())
        await ws.send(json.dumps({"type": "ack", "index": 0, "ok": True}))
        await ws.send(json.dumps({"type": "ack", "index": 1, "ok": False, "reason": "score is not the formula"}))
        got.append(await answer(ws, box, 2))
        await ws.send(json.dumps({"type": "done", "status": "scored", "score": -0.25, "successes": 0, "scenes": 3}))
        await asyncio.sleep(0.2)

    async def main():
        async with FakeServer(script) as srv:
            code = await sv.serve(srv.api, REF, KP, runner(), backoff=FAST, log=logs.append)
            return code, srv

    code, srv = run(main())
    assert code == 0 and srv.conns == 1
    h = srv.hellos[0]
    assert h["hotkey"] == KP.ss58_address and h["image"] == REF
    assert sorted(m["index"] for m in got) == [0, 1, 2]
    assert all(m["type"] == "result" and m["result"]["scene_id"] == f"s{m['index']}" for m in got)
    assert len(pids_of(pids)) == 3, "one process per scene"
    assert any(x.startswith("connected: submission sub-1, status phase1, 0/3 scenes done; the server sends 2 scenes "
                            "at a time; each scene must be answered within 30.0 min") for x in logs), logs
    assert any("scene 2 (s2): done (0/10 picks, 0 out" in x and "claimed score +0.300" in x
               and "3/3 scenes answered, claimed score +0.200 over 3 here (1 accepted, 1 rejected)" in x
               for x in logs), logs
    assert any("scene 1: the server rejected this answer (the scene scores 0): score is not the formula" in x
               for x in logs), logs
    assert any("phase 1 is finished: status scored, score -0.250 (0/3 successes)" in x for x in logs), logs
    assert "done: 3 scenes answered here (done 3; claimed score +0.200)" in logs[-1], logs


def test_runs_outlive_the_connection_and_unacked_results_are_sent_again(pids):
    logs, seen = [], []

    async def script(ws, srv):
        box = Inbox(ws)
        await ws.send(json.dumps({"type": "welcome", "scenes": 2, "done": 0}))
        if srv.conns == 1:
            await ws.send(json.dumps(scene_msg(0)))
            await asyncio.sleep(0.1)
            await ws.close(1011, "server restart")
            return
        if srv.conns == 2:
            seen.append(await box.result())
            await ws.send(json.dumps(scene_msg(0)))
            seen.append(await box.result())
            await ws.close(1011, "server restart again")
            return
        seen.append(await box.result())
        await ws.send(json.dumps({"type": "ack", "index": 0, "ok": True}))
        seen.append(await answer(ws, box, 1))
        await ws.send(json.dumps({"type": "done"}))
        await asyncio.sleep(0.2)

    async def main():
        async with FakeServer(script) as srv:
            code = await sv.serve(srv.api, REF, KP, runner(secs=1.0), backoff=FAST, log=logs.append)
            return code, srv

    code, srv = run(main())
    assert code == 0 and srv.conns == 3, logs
    assert [m["index"] for m in seen] == [0, 0, 0, 1]
    assert len({json.dumps(m["result"], sort_keys=True) for m in seen[:3]}) == 1, "the same answer each time"
    assert len(pids_of(pids)) == 2, "a scene sent again is not run again"
    assert any("reconnecting in" in x and "1 scenes keep running here" in x for x in logs), logs
    assert any("sent 1 answers again that the server had not acknowledged" in x for x in logs), logs


def test_a_claim_that_fails_the_check_is_sent_and_flagged(pids):
    logs, got = [], []

    async def script(ws, srv):
        got.append(await answer(ws, Inbox(ws), 0))
        await ws.send(json.dumps({"type": "done"}))
        await asyncio.sleep(0.2)

    async def main():
        async with FakeServer(script) as srv:
            return await sv.serve(srv.api, REF, KP, runner("bad"), backoff=FAST, log=logs.append)

    assert run(main()) == 0 and got[0]["index"] == 0
    assert any("warning: scene 0: this claim fails the subnet's check (it scores 0): pick 0 score 1" in x
               for x in logs), logs


def test_a_scene_that_cannot_run_here_is_answered_as_harness_error(pids):
    got = []

    async def script(ws, srv):
        got.append(await answer(ws, Inbox(ws), 0))
        await ws.send(json.dumps({"type": "done"}))
        await asyncio.sleep(0.2)

    async def main():
        async with FakeServer(script) as srv:
            return await sv.serve(srv.api, REF, KP, runner("crash"), backoff=FAST)

    assert run(main()) == 0
    r = got[0]["result"]
    assert (r["outcome"], r["infra"], r["score"], r["action_hash"], r["n"]) == ("harness_error", True, None, None, 0)
    assert "exited 3" in r["error"] and "the harness broke" in r["error"]


def test_bad_scene_messages_and_unknown_messages_are_ignored(pids):
    logs, got = [], []

    async def script(ws, srv):
        box = Inbox(ws)
        for m in ({"type": "scene", "index": "x"}, {"type": "scene", "index": 1}, {"type": "ack", "index": 9},
                  {"type": "start", "scene_key": "0.0"}, [1, 2], {"type": "error", "index": 4, "reason": "late"}):
            await ws.send(json.dumps(m))
        await ws.send("not json")
        got.append(await answer(ws, box, 0))
        await ws.send(json.dumps({"type": "done"}))
        await asyncio.sleep(0.2)
        got.append(box.other)

    async def main():
        async with FakeServer(script) as srv:
            return await sv.serve(srv.api, REF, KP, runner(), backoff=FAST, log=logs.append)

    assert run(main()) == 0
    assert got[0]["index"] == 0 and got[1] == [] and len(pids_of(pids)) == 1
    assert sum("ignored a scene message" in x for x in logs) == 2, logs
    assert any("scene 4: the server says: late" in x for x in logs), logs


def test_concurrency_caps_the_runs_here(pids):
    logs, order = [], []

    async def script(ws, srv):
        box = Inbox(ws)
        await ws.send(json.dumps({"type": "welcome", "scenes_at_once": 3, "concurrency": 1}))
        for i in range(3):
            await ws.send(json.dumps(scene_msg(i)))
        for _ in range(3):
            order.append((await box.result())["index"])
        await ws.send(json.dumps({"type": "done"}))
        await asyncio.sleep(0.2)

    async def main():
        async with FakeServer(script) as srv:
            t0 = time.monotonic()
            code = await sv.serve(srv.api, REF, KP, runner(secs=0.4, cap=1), backoff=FAST, log=logs.append)
            return code, time.monotonic() - t0

    code, took = run(main())
    assert code == 0 and sorted(order) == [0, 1, 2] and took >= 1.2, took
    assert any("the server sends 3 scenes at once and --concurrency caps this miner at 1" in x for x in logs), logs


def test_a_refused_hello_stops_without_retrying():
    async def main(refuse):
        async with FakeServer(None, refuse=refuse) as srv:
            code = await sv.serve(srv.api, REF, KP, runner(), backoff=FAST, log=logs.append)
            return code, srv

    logs = []
    code, srv = run(main((4003, "no submission for this hotkey and image")))
    assert code == 1 and len(srv.hellos) == 1
    assert any("refused" in x and "no submission for this hotkey" in x for x in logs), logs


def test_an_unreachable_server_is_retried_then_given_up(tmp_path):
    logs = []

    async def main():
        return await sv.serve("http://127.0.0.1:9", REF, KP, runner(), backoff=FAST, max_attempts=3, log=logs.append)

    assert run(main()) == 2
    assert sum("reconnecting in" in x for x in logs) == 2 and "giving up after 3 attempts" in logs[-1], logs


def test_stop_closes_the_socket_and_kills_every_run(pids):
    async def script(ws, srv):
        await ws.send(json.dumps(scene_msg(0)))
        await ws.send(json.dumps(scene_msg(1)))
        await ws.wait_closed()

    async def main():
        async with FakeServer(script) as srv:
            stop = asyncio.Event()
            task = asyncio.ensure_future(sv.serve(srv.api, REF, KP, runner("hang"), backoff=FAST, stop=stop))
            while srv.conns == 0 or len(pids_of(pids)) < 2:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.2)
            stop.set()
            code = await task
            await asyncio.sleep(0.1)
            return code, srv

    code, srv = run(main())
    assert code == 0 and srv.closes == [1000]
    assert len(pids_of(pids)) == 2 and not any(alive(p) for p in pids_of(pids))


def test_sigterm_shuts_the_cli_down_cleanly(pids, tmp_path):
    ready = tmp_path / "server.json"
    srv_code = f'''
import asyncio, json, sys
sys.path.insert(0, {str(HERE)!r})
from test_serve import FakeServer, scene_msg

async def script(ws, srv):
    await ws.send(json.dumps(scene_msg(0)))
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
from vector_miner.skills.bin_picking.expert import serve as sv
r = sv.Runner({{"command": ["fake", "hang", "0"]}}, argv=[sys.executable, {str(HERE / "fake_play.py")!r}])
sys.exit(sv.run({api!r}, {REF!r}, Keypair.create_from_uri("//Alice"), r))
'''
        p = subprocess.Popen([sys.executable, "-c", miner], stderr=subprocess.PIPE, text=True)
        while len(pids_of(pids)) < 1:
            assert time.time() - t0 < 30 and p.poll() is None
            time.sleep(0.05)
        p.send_signal(signal.SIGTERM)
        _, err = p.communicate(timeout=30)
        assert p.returncode == 0, err
        assert "stopped: 0 scenes answered here" in err
        server.wait(timeout=20)
        assert json.loads((tmp_path / "closes").read_text()) == [1000]
        assert len(pids_of(pids)) == 1 and not any(alive(x) for x in pids_of(pids))
    finally:
        server.kill()


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "docker.log"
    b = tmp_path / "docker"
    b.write_text(f"#!/bin/sh\nFAKE_DOCKER_LOG={log} exec {sys.executable} {HERE / 'fake_docker.py'} \"$@\"\n")
    b.chmod(0o755)
    return b, log


def test_cli_serve_checks_its_arguments(monkeypatch, capsys):
    pytest.importorskip("vector_env.harness")
    from vector_miner.skills.bin_picking.expert import cli
    monkeypatch.delenv("VECTOR_MINER_API", raising=False)
    with pytest.raises(SystemExit, match="serve the image you committed"):
        cli.main(["serve", "--image", "ghcr.io/a/b:latest", "--api", "http://x"])
    with pytest.raises(SystemExit, match="--api"):
        cli.main(["serve", "--image", REF, "--command", "true"])
    with pytest.raises(SystemExit, match="--concurrency"):
        cli.main(["serve", "--image", REF, "--command", "true", "--api", "http://x", "--concurrency", "0"])
    monkeypatch.setattr(cli, "_keypair", lambda a: KP)
    seen = {}
    monkeypatch.setattr(sv, "run", lambda api, ref, kp, r, **k: seen.update(api=api, ref=ref, k=k, ex=r.expert,
                                                                             cap=r.cap) or 0)
    assert cli.main(["serve", "--image", REF, "--command", "node solve.mjs", "--api", "http://x", "--concurrency", "3"]) == 0
    assert seen == {"api": "http://x", "ref": REF, "k": {}, "ex": {"command": ["node", "solve.mjs"]}, "cap": 3}
    capsys.readouterr()
    assert cli.main(["serve", "--image", REF, "--command", "node solve.mjs", "--api", "http://x", "--warm", "2"]) == 0
    err = capsys.readouterr().err
    assert seen["cap"] is None and "--warm has no effect now" in err and "as many at a time as the server sends" in err


def test_cli_serve_image_needs_it_locally(monkeypatch, capsys, tmp_path):
    pytest.importorskip("vector_env.harness")
    from vector_miner.skills.bin_picking.expert import cli
    monkeypatch.setattr(cli, "_keypair", lambda a: KP)
    assert cli.main(["serve", "--image", REF, "--api", "http://x", "--docker", str(tmp_path / "none")]) == 2
    assert "cannot run" in capsys.readouterr().err


@pytest.fixture(scope="module")
def real_scenes():
    assets = pytest.importorskip("vector_env.assets")
    if assets.find() is None:
        pytest.skip(assets.MISSING)
    import copy

    from vector_env import scenes
    classic = scenes.generate(1, {"mode": "classic"})
    pile = copy.deepcopy(classic)
    pile["meta"]["config"]["rules"].update(mode="pile", picks=3, target={"min_px": 10, "buried_min_px": 5})
    pile["scene_id"] += "-picks"
    return [classic, pile]


def _limits(sc, time_limit_s):
    from vector_env import rules
    t = rules.task(time_limit_s)
    return {"time_limit_s": time_limit_s, "picks": rules.scene_picks(sc), "max_rows": t["max_rows"],
            "tick_s": t["tick_s"], "control_hz": t["control_hz"], "wall_s": 60.0, "score": {"out_unit": 2.0}}


@pytest.mark.parametrize("kind", ["command", "docker"])
def test_real_scenes_run_with_the_harness_and_pass_the_servers_check(real_scenes, kind, tmp_path):
    from vector_env import harness
    got, lims = {}, [_limits(real_scenes[0], 3.0), _limits(real_scenes[1], 1.5)]

    async def script(ws, srv):
        box = Inbox(ws)
        await ws.send(json.dumps({"type": "welcome", "scenes_at_once": 2, "scenes": 2}))
        for i, (sc, lim) in enumerate(zip(real_scenes, lims)):
            await ws.send(json.dumps({"type": "scene", "index": i, "scene": sc, "limits": lim}))
        for _ in range(2):
            m = await box.result(240)
            got[m["index"]] = m["result"]
        await ws.send(json.dumps({"type": "done"}))
        await asyncio.sleep(0.2)

    if kind == "docker":
        b, log = _fake_docker(tmp_path)
        r = sv.Runner({"image": REF}, {"binary": str(b)}, tmp_path / "err")
    else:
        r = sv.Runner(expert("track", 5), None, tmp_path / "err")

    async def main():
        async with FakeServer(script) as srv:
            return await sv.serve(srv.api, REF, KP, r, backoff=FAST)

    assert run(main(), 400) == 0
    for i, (sc, lim) in enumerate(zip(real_scenes, lims)):
        claim = got[i]
        assert harness.check_claim(sc, claim, lim) == [], harness.check_claim(sc, claim, lim)
        assert claim["outcome"] in ("done", "timed_out", "missed"), claim
        assert claim["expert_s"] > 0
        local = harness.run(expert("track", 3 if kind == "docker" else 5), [sc], time_limit_s=lim["time_limit_s"],
                            wall_s=60)[0]
        assert local["action_hash"] == claim["action_hash"], "the same run as vector-env's harness here"
    assert got[1]["score"] == harness.wire_result(got[1], lims[1])["score"]
    logs = sorted(p.name for p in (tmp_path / "err").iterdir())
    assert logs == sorted(f"{i:04d}-{sc['scene_id']}.log" for i, sc in enumerate(real_scenes)), logs
    if kind == "docker":
        runs = [json.loads(x) for x in log.read_text().splitlines()]
        runs = [c for c in runs if c[0] == "run"]
        assert len(runs) == 2
        for c in runs:
            flat = " ".join(c)
            for flag in ("--network none", "--read-only", "--cap-drop ALL", "no-new-privileges", "--pull never",
                         f"--label vector-miner.serve={r.tag}", ":/bundle:ro"):
                assert flag in flat, flag
            assert c[-1] == REF


def test_busy_and_not_open_with_retry_at_are_retried_and_real_refusals_stop(pids):
    logs = []
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=0.05)).isoformat()
    got = []

    async def script(ws, srv):
        box = Inbox(ws)
        await ws.send(json.dumps({"type": "welcome", "submission": "sub-1", "status": "phase1", "done": 0, "scenes": 1}))
        got.append(await answer(ws, box, 0))
        await ws.send(json.dumps({"type": "done", "submission": "sub-1", "status": "scored", "score": 0.0,
                                  "successes": 0, "scenes": 1}))
        await asyncio.sleep(0.2)

    refusals = [({"reason": "busy", "message": "every phase-1 slot is in use", "retry_after_s": 0.02}, 4429,
                 "busy: every phase-1 slot is in use"),
                (None, 4429, "busy: too many miners"),
                ({"reason": "not_open", "message": "phase 1 hit failures on our side", "submission": "sub-1",
                  "status": "review", "retry_at": soon}, 4403, "not_open: phase 1 hit failures on our side")]

    async def main(refusals, **kw):
        async with FakeServer(script, refusals=refusals) as srv:
            code = await sv.serve(srv.api, REF, KP, runner(), backoff=FAST, log=logs.append, **kw)
            return code, srv

    sv_busy = sv.BUSY_WAIT_S
    sv.BUSY_WAIT_S = 0.02
    try:
        code, srv = run(main(refusals))
    finally:
        sv.BUSY_WAIT_S = sv_busy
    assert code == 0 and srv.conns == 1 and len(srv.hellos) == 4, logs
    assert got[0]["index"] == 0
    assert any("turned the connection away for now (close 4429: busy: every phase-1 slot" in x for x in logs), logs
    assert any("it opens again at " + soon in x for x in logs), logs
    assert any(x.startswith("connected: submission sub-1, status phase1, 0/1 scenes done") for x in logs), logs
    assert any("phase 1 is finished: status scored, score 0.000 (0/1 successes)" in x for x in logs), logs
    logs.clear()
    code, srv = run(main([({"reason": "image", "message": "hello.image is not the committed image (ghcr.io/x)"}, 4403,
                           "image: hello.image is not the committed image (ghcr.io/x)")]))
    assert code == 1 and len(srv.hellos) == 1
    assert any("refused this miner (close 4403): image: hello.image is not the committed image" in x for x in logs), logs
    logs.clear()
    code, srv = run(main([({"reason": "not_open", "message": "phase 1 of submission sub-1 is over (status ranked)",
                            "status": "ranked"}, 4403, "not_open: phase 1 of submission sub-1 is over")]))
    assert code == 1 and len(srv.hellos) == 1 and any("is over (status ranked)" in x for x in logs), logs
    logs.clear()
    nonce = ({"reason": "bad_hello", "message": "unknown or expired nonce (GET /v1/expert/nonce)"}, 4401,
             "bad_hello: unknown or expired nonce (GET /v1/expert/nonce)")
    code, srv = run(main([nonce] * (sv.NONCE_RETRIES + 1)))
    assert code == 1 and len(srv.hellos) == sv.NONCE_RETRIES + 1, logs


def test_a_review_at_the_end_of_phase1_waits_for_retry_at(pids):
    logs = []
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=0.05)).isoformat()

    async def script(ws, srv):
        await ws.send(json.dumps({"type": "error", "reason": "notice", "message": "just saying"}))
        if srv.conns == 1:
            await ws.send(json.dumps({"type": "done", "submission": "sub-1", "status": "review", "score": None,
                                      "retry_at": soon}))
        else:
            await ws.send(json.dumps({"type": "done", "submission": "sub-1", "status": "scored", "score": 0.5,
                                      "successes": 1, "scenes": 2}))
        await asyncio.sleep(0.2)

    async def main():
        async with FakeServer(script) as srv:
            code = await sv.serve(srv.api, REF, KP, runner(), backoff=FAST, log=logs.append)
            return code, srv

    code, srv = run(main())
    assert code == 0 and srv.conns == 2, logs
    assert any("the server says: notice: just saying" in x for x in logs), logs
    assert any("phase 1 runs again from " + soon in x and "keep serve running" in x for x in logs), logs


def test_a_scene_index_sent_with_another_scene_runs_that_scene(pids):
    got = []

    async def script(ws, srv):
        box = Inbox(ws)
        await ws.send(json.dumps(scene_msg(0, "old")))
        got.append(await box.result())
        await ws.send(json.dumps(scene_msg(0, "new")))
        got.append(await box.result())
        await ws.send(json.dumps({"type": "done"}))
        await asyncio.sleep(0.2)

    async def main():
        async with FakeServer(script) as srv:
            return await sv.serve(srv.api, REF, KP, runner(), backoff=FAST)

    assert run(main()) == 0
    assert [m["result"]["scene_id"] for m in got] == ["old", "new"] and len(pids_of(pids)) == 2


def test_a_leaf_that_is_not_running_waits_for_retry_at_or_stops(pids):
    logs = []
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=0.05)).isoformat()
    got = []

    async def script(ws, srv):
        box = Inbox(ws)
        await ws.send(json.dumps({"type": "welcome", "submission": "sub-1", "status": "phase1", "done": 0, "scenes": 1}))
        got.append(await answer(ws, box, 0))
        await ws.send(json.dumps({"type": "done", "submission": "sub-1", "status": "scored", "score": 0.0,
                                  "successes": 0, "scenes": 1}))
        await asyncio.sleep(0.2)

    async def main(refusals):
        async with FakeServer(script, refusals=refusals) as srv:
            code = await sv.serve(srv.api, REF, KP, runner(), backoff=FAST, log=logs.append)
            return code, srv

    planned = ({"reason": "not_running", "message": "the competition is planned", "retry_at": soon}, 4403,
               "not_running: the competition is planned")
    code, srv = run(main([planned]))
    assert code == 0 and srv.conns == 1 and len(srv.hellos) == 2, logs
    assert got[0]["index"] == 0
    assert any("turned the connection away for now (close 4403: not_running: the competition is planned)" in x
               and "it opens again at " + soon in x for x in logs), logs
    logs.clear()
    for status in ("finished", "paused"):
        code, srv = run(main([({"reason": "not_running", "message": f"the competition is {status}"}, 4403,
                               f"not_running: the competition is {status}")]))
        assert code == 1 and srv.conns == 0 and len(srv.hellos) == 1, logs
        assert any(f"refused this miner (close 4403): not_running: the competition is {status}" in x for x in logs), logs
        logs.clear()
