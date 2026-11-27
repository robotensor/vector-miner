from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from vector_miner import cli, skills, wallet
from vector_miner.skills.bin_picking import expert

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "vector_miner"
OLD = ("board", "local", "serve", "subverify", "verify")
LEAF_CMDS = ("commit", "status", "fee", "serve", "eval", "verify")


@pytest.fixture
def no_env(monkeypatch):
    monkeypatch.delenv("VECTOR_MINER_LEAF", raising=False)


@pytest.fixture
def second_leaf(tmp_path, monkeypatch, no_env):
    """A second leaf package (assembly-policy) next to the installed ones."""
    leaf = tmp_path / "assembly" / "policy"
    leaf.mkdir(parents=True)
    (tmp_path / "assembly" / "__init__.py").write_text("")
    (leaf / "__init__.py").write_text('ID = "assembly-policy"\nSKILL = "assembly"\nKIND = "policy"\n')
    (leaf / "cli.py").write_text('def main(argv=None, prog="vector-miner"):\n'
                                 '    print("assembly-policy", prog, list(argv or []))\n'
                                 '    return 7\n')
    (tmp_path / "notaskill.py").write_text("")
    (tmp_path / "broken" / "expert").mkdir(parents=True)
    (tmp_path / "broken" / "__init__.py").write_text("")
    (tmp_path / "broken" / "expert" / "__init__.py").write_text('ID = "other-expert"\nSKILL = "other"\nKIND = "expert"\n')
    monkeypatch.setattr(skills, "__path__", [*skills.__path__, str(tmp_path)])
    yield
    for name in [m for m in sys.modules if m.startswith(("vector_miner.skills.assembly", "vector_miner.skills.broken",
                                                          "vector_miner.skills.notaskill"))]:
        del sys.modules[name]


def test_the_only_leaf_is_the_default(no_env, capsys):
    assert list(skills.leaves()) == ["bin-picking-expert"]
    assert skills.leaf("bin-picking-expert") is expert
    assert (expert.ID, expert.SKILL, expert.KIND) == ("bin-picking-expert", "bin-picking", "expert")
    assert skills.default() == "bin-picking-expert"
    assert cli.main(["leaves"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "leaves": [{"id": "bin-picking-expert", "skill": "bin-picking", "kind": "expert"}], "default": "bin-picking-expert"}
    with pytest.raises(KeyError) as e:
        skills.leaf("nope")
    assert e.value.args[0] == "unknown leaf nope (installed: bin-picking-expert)"


def test_leaves_prints_one_json_line_from_the_command_line(no_env):
    p = subprocess.run([sys.executable, "-m", "vector_miner.cli", "leaves"], cwd=ROOT, capture_output=True, text=True,
                       env={k: v for k, v in __import__("os").environ.items() if k != "VECTOR_MINER_LEAF"}, timeout=60)
    assert p.returncode == 0 and p.stderr == "" and len(p.stdout.splitlines()) == 1
    assert json.loads(p.stdout)["default"] == "bin-picking-expert"


def test_leaf_flag_and_env_choose_the_leaf(monkeypatch, no_env, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--leaf", "bin-picking-expert", "fee"])
    assert e.value.code == 2 and "vector-miner fee: error" in capsys.readouterr().err
    assert cli.main(["--leaf=bin-picking-expert", "verify", "--doc", "x.json"]) == 2
    assert "give --submission ID" in capsys.readouterr().err
    monkeypatch.setenv("VECTOR_MINER_LEAF", "bin-picking-expert")
    assert skills.default() == "bin-picking-expert"
    assert cli.main(["verify", "--doc", "x.json"]) == 2
    assert "give --submission ID" in capsys.readouterr().err
    monkeypatch.setenv("VECTOR_MINER_LEAF", "nope")
    assert cli.main(["verify", "--doc", "x.json"]) == 2
    assert capsys.readouterr().err == "vector-miner: unknown leaf nope (installed: bin-picking-expert)\n"
    assert cli.main(["--leaf", "bin-picking-expert", "verify", "--doc", "x.json"]) == 2
    assert "give --submission ID" in capsys.readouterr().err
    assert cli.main(["--leaf"]) == 2
    assert "argument --leaf: expected one argument" in capsys.readouterr().err


def test_the_leaf_flag_is_read_only_before_the_command(no_env, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["status", "--api", "http://api", "--hotkey", "5x", "--leaf", "bin-picking-expert"])
    assert e.value.code == 2 and "unrecognized arguments: --leaf" in capsys.readouterr().err


def test_unknown_and_several_leaves_exit_2(second_leaf, monkeypatch, capsys):
    assert list(skills.leaves()) == ["assembly-policy", "bin-picking-expert"]
    assert skills.default() is None
    assert cli.main(["status"]) == 2
    assert capsys.readouterr().err == ("vector-miner: several leaves are installed (assembly-policy, bin-picking-expert): "
                                       "give --leaf ID or set VECTOR_MINER_LEAF\n")
    assert cli.main([]) == 2
    assert "several leaves are installed" in capsys.readouterr().err
    assert cli.main(["--leaf", "x-expert", "status"]) == 2
    assert capsys.readouterr().err == "vector-miner: unknown leaf x-expert (installed: assembly-policy, bin-picking-expert)\n"
    assert cli.main(["--leaf", "assembly-policy", "status", "--x"]) == 7
    assert capsys.readouterr().out == "assembly-policy vector-miner ['status', '--x']\n"
    monkeypatch.setenv("VECTOR_MINER_LEAF", "assembly-policy")
    assert cli.main(["eval"]) == 7
    capsys.readouterr()
    assert cli.main(["leaves"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "leaves": [{"id": "assembly-policy", "skill": "assembly", "kind": "policy"},
                   {"id": "bin-picking-expert", "skill": "bin-picking", "kind": "expert"}], "default": "assembly-policy"}
    monkeypatch.delenv("VECTOR_MINER_LEAF")
    assert cli.main(["-h"]) == 0
    out = capsys.readouterr().out
    assert "usage: vector-miner [--leaf ID] <command>" in out and "assembly-policy" in out and "bin-picking-expert" in out


def test_work_is_gone(no_env):
    with pytest.raises(SystemExit):
        cli.main(["work", "--api", "http://api"])


def test_help_lists_the_leaf_commands(no_env, capsys):
    for argv in (["-h"], ["--help"], ["--leaf", "bin-picking-expert", "-h"]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 0
        out = capsys.readouterr().out
        assert out.startswith("usage: vector-miner [-h] {commit,status,fee,serve,eval,verify} ...")
        assert all(c in out for c in LEAF_CMDS)


def test_each_old_path_is_the_leaf_module():
    for name in OLD:
        old = importlib.import_module(f"vector_miner.{name}")
        new = importlib.import_module(f"vector_miner.skills.bin_picking.expert.{name}")
        assert old is new and sys.modules[f"vector_miner.{name}"] is new
    from vector_miner import serve, subverify
    assert serve.Runner is importlib.import_module("vector_miner.skills.bin_picking.expert.serve").Runner
    assert subverify.verify is importlib.import_module("vector_miner.skills.bin_picking.expert.subverify").verify


def test_the_old_path_loads_from_a_fresh_interpreter():
    code = ("import sys; sys.path.insert(0, %r); import vector_miner.subverify as a, "
            "vector_miner.skills.bin_picking.expert.subverify as b; assert a is b; print('same')" % str(ROOT))
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd="/")
    assert p.returncode == 0 and p.stdout == "same\n", p.stderr


def test_a_threaded_first_import_of_an_old_path_resolves_every_name():
    code = ("import sys, threading; sys.path.insert(0, %r)\n"
            "b = threading.Barrier(4); errs = []\n"
            "def go():\n"
            "    b.wait()\n"
            "    try:\n"
            "        from vector_miner.subverify import verify\n"
            "        from vector_miner.serve import Runner\n"
            "    except Exception as e:\n"
            "        errs.append(repr(e))\n"
            "ts = [threading.Thread(target=go) for _ in range(4)]\n"
            "[t.start() for t in ts]; [t.join() for t in ts]\n"
            "print(errs or 'ok')" % str(ROOT))
    for _ in range(3):
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd="/")
        assert p.returncode == 0 and p.stdout == "ok\n", (p.stdout, p.stderr)


def test_btcli_create_text(tmp_path):
    msg = wallet.btcli_create("miner", "hk", str(tmp_path))
    assert msg == (f".venv/bin/btcli wallet new-coldkey --wallet miner --wallet-path {tmp_path}, then "
                   f".venv/bin/btcli wallet new-hotkey --wallet miner --wallet-hotkey hk --wallet-path {tmp_path}")
    (tmp_path / "miner").mkdir()
    (tmp_path / "miner" / "coldkeypub.txt").write_text("{}")
    assert wallet.btcli_create("miner", "hk", str(tmp_path)) == (
        f".venv/bin/btcli wallet new-hotkey --wallet miner --wallet-hotkey hk --wallet-path {tmp_path}")
    assert wallet.btcli_create("nobody-x", "hk", wallet.DEFAULT_WALLETS).endswith(
        "new-hotkey --wallet nobody-x --wallet-hotkey hk")


def test_wallet_flags_and_common():
    a = wallet.common().parse_args([])
    assert (a.wallet_name, a.wallet_hotkey, a.wallet_path) == ("default", "default", wallet.DEFAULT_WALLETS)
    assert wallet.wallet_flags(a) == ""
    a = wallet.common().parse_args(["--wallet.name", "cold", "--wallet.hotkey", "hk1", "--wallet.path", "/w"])
    assert wallet.wallet_flags(a) == " --wallet.name cold --wallet.hotkey hk1 --wallet.path /w"


def test_shared_code_names_no_leaf():
    names = re.compile(r"bin_picking|bin-picking|/v1/skills|/v1/expert|/v1/dev")
    aliases = {PKG / f"{n}.py" for n in OLD}
    hits = [f"{p.relative_to(ROOT)}:{i}" for p in sorted(PKG.rglob("*.py"))
            if p not in aliases and "skills" not in p.relative_to(PKG).parts[:-1]
            for i, line in enumerate(p.read_text().splitlines(), 1) if names.search(line)]
    assert hits == []
    assert (PKG / "skills" / "__init__.py").read_text().count("bin") == 0
    for n in OLD:
        assert (PKG / f"{n}.py").read_text() == (
            f'"""Old import path of vector_miner.skills.bin_picking.expert.{n}."""\nimport sys\n\n'
            f"from .skills.bin_picking.expert import {n} as _module\n\n\n"
            "def __getattr__(name):\n    return getattr(_module, name)\n\n\nsys.modules[__name__] = _module\n")

