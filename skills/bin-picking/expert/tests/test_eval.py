from __future__ import annotations

import json
import sys

import pytest
from conftest import HERE, _no_docker_scenes

from vector_miner.skills.bin_picking.expert import cli


def test_eval_runs_the_local_harness(capsys, tmp_path):
    pytest.importorskip("vector_env")
    from vector_env import assets, harness, scenes
    from vector_env.settings import env_config
    try:
        assets.ensure()
    except Exception as e:
        pytest.skip(f"no asset bundle: {e}")
    cmd = f"node {HERE.parent / 'example' / 'solve.mjs'}"
    rows = tmp_path / "results.jsonl"
    wall = 20 * float(env_config()["harness"]["wall_s"])
    assert cli.main(["eval", "--command", cmd, "--scenes", "2", "--seed", "1", "--workers", "2", "--wall", str(wall),
                     "--results", str(rows)]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    res = [json.loads(x) for x in rows.read_text().splitlines()]
    assert out["scenes"] == 2 and out["wall_s"] == wall and out["successes"] == sum(r["success"] for r in res), out
    assert out["score"] == out["successes"] / 2, out
    for i, r in enumerate(res):
        assert r["wall_s"] < wall and r["outcome"] != "wall_timeout", r
        sc, _ = scenes.draw_index(1, i)
        rp = harness.replay(sc, r["ctrl"], r["n"], time_limit_s=out["time_limit_s"], result=r)
        assert (rp["outcome"], rp["success"], rp["t"]) == (r["outcome"], r["success"], r["t"])


def test_eval_image_without_docker_is_an_infra_error(monkeypatch, capsys, tmp_path):
    _no_docker_scenes(monkeypatch)
    assert cli.main(["eval", "--image", "ghcr.io/a/b:1", "--docker", str(tmp_path / "none"), "--scenes", "3"]) == 2
    cap = capsys.readouterr()
    assert cap.out == "" and '"score"' not in cap.err
    line = cap.err.strip().splitlines()[-1]
    assert line.startswith("vector-miner: no scene ran here (docker_unavailable") and "--command" in line


def test_eval_pull_without_docker_is_one_line(monkeypatch, capsys, tmp_path):
    _no_docker_scenes(monkeypatch)
    assert cli.main(["eval", "--image", "ghcr.io/a/b:1", "--pull", "--docker", str(tmp_path / "none")]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and err[0].startswith("vector-miner: cannot pull ghcr.io/a/b:1: no docker binary")


def test_eval_skips_like_the_round(monkeypatch, capsys):
    from vector_env import harness, scenes
    from vector_env.compose import SceneError
    bad = {scenes.seed_for(2, 0) + k for k in range(scenes.SEED_TRIES)}

    def gen(seed, draw):
        if seed in bad:
            raise SceneError("no part in the bin is visible enough to be a target")
        return {"scene_id": f"s{seed}", "seed": seed, "meta": {"parts": [1]}}
    monkeypatch.setattr(scenes, "generate", gen)
    def hrun(ex, scs, **k):
        return [{"scene_id": s["scene_id"], "outcome": "success", "success": True, "t": 3.0, "n": 60, "wall_s": 1.0,
                 "infra": False} for s in scs]
    monkeypatch.setattr(harness, "run", hrun)
    assert cli.main(["eval", "--command", "true", "--scenes", "3", "--seed", "2"]) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert out["skipped"] == [0] and out["scenes"] == 2 and out["score"] == 1.0 and "Traceback" not in cap.err


def test_eval_passes_trace_and_scene_cache(monkeypatch, capsys, tmp_path):
    from vector_env import cli as env_cli

    from vector_miner.skills.bin_picking.expert import local
    seen = {}

    def evaluate(expert, n, seed, **k):
        seen.update(k)
        raise env_cli.EvalError("stop here")
    monkeypatch.setattr(local, "evaluate", evaluate)
    assert cli.main(["eval", "--command", "true", "--trace", str(tmp_path / "t"), "--scene-cache", str(tmp_path / "c"),
                     "--stderr", str(tmp_path / "e"), "--workers", "3"]) == 2
    assert seen["trace_dir"] == str(tmp_path / "t") and seen["scene_cache_dir"] == str(tmp_path / "c")
    assert seen["stderr_dir"] == str(tmp_path / "e") and seen["workers"] == 3
    assert capsys.readouterr().err.strip() == "vector-miner: stop here"
    assert cli.main(["eval", "--command", "true", "--no-scene-cache"]) == 2
    assert seen["scene_cache_dir"] is False and seen["trace_dir"] is None and "limits" not in seen
    capsys.readouterr()
    assert cli.main(["eval", "--command", "true", "--step-deadline", "0.5", "--first-step-deadline", "3"]) == 2
    assert "have no effect now" in capsys.readouterr().err


def _det_run(monkeypatch, hashes):
    from vector_env import harness
    _no_docker_scenes(monkeypatch)
    calls = []

    def hrun(ex, scs, **k):
        calls.append([s["scene_id"] for s in scs])
        n = len(calls) - 1
        return [{"scene_id": s["scene_id"], "outcome": hashes[n][s["scene_id"]][1], "success": False, "t": 0.0, "n": 3,
                 "wall_s": 1.0, "infra": hashes[n][s["scene_id"]][1] == "start_failed",
                 "action_hash": hashes[n][s["scene_id"]][0]} for s in scs]
    monkeypatch.setattr(harness, "run", hrun)
    return calls


def test_eval_determinism_runs_twice_and_compares_action_hashes(monkeypatch, capsys):
    ids = [f"s{i}-0000000{i}" for i in range(3)]
    same = {i: ("%016x" % k, "missed") for k, i in enumerate(ids)}
    calls = _det_run(monkeypatch, [same, same])
    assert cli.main(["eval", "--command", "true", "--determinism", "3"]) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out.strip().splitlines()[-1])
    assert calls == [ids, ids[::-1]], "the second run goes in reverse order"
    assert out["determinism"] == {"compared": 3, "equal": 3, "all_equal": True, "mismatched": [], "infra": 0}
    assert "deterministic on 3 scenes" in cap.err
    calls = _det_run(monkeypatch, [same, {**same, ids[1]: ("f" * 16, "missed")}])
    assert cli.main(["eval", "--command", "true", "--determinism", "3"]) == 1
    cap = capsys.readouterr()
    assert json.loads(cap.out.strip().splitlines()[-1])["determinism"]["mismatched"] == [ids[1]]
    assert "NOT deterministic: 1 of 3" in cap.err and "seed every random draw" in cap.err
    _det_run(monkeypatch, [same, {**same, ids[2]: (None, "start_failed")}])
    assert cli.main(["eval", "--command", "true", "--determinism", "3"]) == 2
    assert "not a determinism result" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="at least 1"):
        cli.main(["eval", "--command", "true", "--determinism", "0"])


def test_eval_determinism_on_real_scenes(capsys):
    pytest.importorskip("vector_env")
    from vector_env import assets
    if assets.find() is None:
        pytest.skip(assets.MISSING)
    base = f"{sys.executable} {HERE / 'fake_expert.py'}"
    assert cli.main(["eval", "--command", f"{base} track 5", "--determinism", "2", "--seed", "3", "--workers", "2",
                     "--time-limit", "3"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["determinism"]["all_equal"] and out["determinism"]["compared"] == 2
    assert cli.main(["eval", "--command", f"{base} noisy 5", "--determinism", "2", "--seed", "3", "--workers", "2",
                     "--time-limit", "3"]) == 1
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert len(out["determinism"]["mismatched"]) == 2


def test_the_expert_time_report_flags_picks_over_the_wall():
    from vector_miner.skills.bin_picking.expert import local
    res = [{"scene_id": "a", "picks": [{"pick": 0, "expert_s": 3.0}, {"pick": 1, "expert_s": 50.0}]},
           {"scene_id": "b", "expert_s": 1.0}, {"scene_id": "c", "outcome": "harness_error"}]
    et = local.expert_time(res, 45.0)
    assert (et["picks"], et["pick_max_s"], et["pick_mean_s"], et["over_scenes"]) == (3, 50.0, 18.0, ["a"])
    assert et["over"] == [{"scene_id": "a", "pick": 1, "s": 50.0}]
    line = local.report_lines(et)[0]
    assert line.startswith("warning: 1 picks used up the wall budget") and "a pick 1: 50 s" in line
    ok = local.expert_time(res[1:], 45.0)
    assert local.report_lines(ok) == ["expert time: at most 1 s in one pick, mean 1 s (the limit in phase 2 is the "
                                      "wall budget of a pick, 45 s here)"]
    assert local.report_lines(local.expert_time([], 45.0)) == []


def test_eval_reports_the_expert_time_on_real_scenes(capsys, tmp_path):
    pytest.importorskip("vector_env")
    from vector_env import assets
    if assets.find() is None:
        pytest.skip(assets.MISSING)
    base = f"{sys.executable} {HERE / 'fake_expert.py'}"
    argv = ["eval", "--seed", "3", "--scenes", "2", "--workers", "2", "--time-limit", "1"]
    assert cli.main([*argv, "--command", f"{base} track 5", "--strict", "--stderr", str(tmp_path / "e")]) == 0
    cap = capsys.readouterr()
    logs = sorted(p.name for p in (tmp_path / "e").iterdir())
    assert len(logs) == 2 and all(x[:5] in ("0000-", "0001-") and x.endswith(".log") for x in logs), logs
    et = json.loads(cap.out.strip().splitlines()[-1])["expert_time"]
    assert et["pick_max_s"] < 5 and et["over_scenes"] == [] and "expert time: at most" in cap.err
    assert cli.main([*argv, "--command", f"{base} lag 2", "--wall", "3"]) == 0
    cap = capsys.readouterr()
    assert "used up the wall budget" in cap.err and "--strict" in cap.err, cap.err
    assert cli.main([*argv, "--command", f"{base} lag 2", "--wall", "3", "--strict"]) == 1
