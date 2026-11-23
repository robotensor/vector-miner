from __future__ import annotations

import hashlib
import json

import pytest

from vector_miner import cli, verify

SALT = "5e" * 16
ENV = {"version": "0.1.0", "assets_version": "2026.11.1", "assets_sha256": "e" * 64}


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


def test_the_round_option_is_gone(capsys):
    with pytest.raises(SystemExit):
        cli.main(["verify", "--round", "4", "--doc", "x.json"])
    assert "--round" in capsys.readouterr().err
    assert cli.main(["verify", "--doc", "x.json"]) == 2
    assert "give --submission ID" in capsys.readouterr().err


def test_documents_are_strict_json():
    with pytest.raises(verify.VerifyError, match="NaN"):
        verify.loads('{"a": NaN}')
    with pytest.raises(verify.VerifyError, match="not a verify document"):
        verify.loads("[1, 2]")
    with pytest.raises(verify.VerifyError, match="not JSON"):
        verify.loads("[" * 100000 + "]" * 100000)
