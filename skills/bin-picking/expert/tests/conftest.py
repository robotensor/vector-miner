from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from bittensor_wallet import Keypair

from vector_miner import api as api_mod

HERE = Path(__file__).resolve().parent
REF = "ghcr.io/alice/bin-expert@sha256:" + "a" * 64
D = "sha256:" + "a" * 64
HOT = Keypair.create_from_uri("//Alice").ss58_address
SALT = "5e" * 16
ENV = {"version": "0.2.0", "assets_version": "2026.11.1", "assets_sha256": "e" * 64}


@pytest.fixture(autouse=True)
def _scene_cache(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("VECTOR_ENV_SCENE_CACHE", str(tmp_path_factory.mktemp("scene-cache")))


def _api(monkeypatch, handler):
    monkeypatch.setattr(api_mod, "make_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(handler)))


def _subs(rows):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/skills/bin-picking/expert/submissions":
            return httpx.Response(200, json={"submissions": [r for r in rows if r["hotkey"] == req.url.params["hotkey"]]})
        return httpx.Response(404, json={"detail": "no"})
    return handler


def _no_docker_scenes(monkeypatch):
    from vector_env import cli as env_cli
    scs = [{"scene_id": f"s{i}-0000000{i}", "seed": i, "xml": "<mujoco/>", "meta": {"parts": [1]}, "checkpoint": "",
            "state_hash": str(i) * 8, "target": 1} for i in range(3)]
    monkeypatch.setattr(env_cli, "draw_scenes", lambda n, seed, draw=None, log=None, **k: (scs[:n], []))


def _h(*parts) -> float:
    return int.from_bytes(hashlib.sha256(":".join(map(str, parts)).encode()).digest()[:6], "big") / float(1 << 48)


class Scenes:
    def __init__(self, broken=(), env=None):
        self.broken, self.env, self.calls = set(broken), dict(env or ENV), []

    def generate(self, seed, draw=None):
        self.calls.append(seed)
        if seed in self.broken:
            raise ValueError("could not place the drawn parts")
        sh = hashlib.sha256(f"state:{seed}:{json.dumps(draw)}".encode()).hexdigest()[:16]
        return {"scene_id": f"s{seed}-{sh[:8]}", "seed": int(seed), "state_hash": sh, "meta": {"parts": [1, 2, 3]}}

    def env_info(self):
        return dict(self.env)
