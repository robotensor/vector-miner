"""scripts/setup.sh: the leaf folder, the round tag pins of vector-env and vector-sim, and the steps after them."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEAF = Path("skills") / "bin-picking" / "expert"
ARG = re.compile(r"^ARG VECTOR_SIM_REF=(\S+)", re.M)


def _bin(d: Path, name: str, body: str) -> None:
    p = d / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(0o755)


def _round(dockerfile: Path) -> str:
    return ARG.search(dockerfile.read_text()).group(1)


def _repo(tmp_path: Path, leaves: dict[str, str | None] | None = None) -> Path:
    """A vector-miner checkout with scripts/setup.sh, pyproject.toml and one example/Dockerfile per leaf folder.

    leaves maps a leaf folder to the round tag of its Dockerfile; None copies this checkout's Dockerfile."""
    repo = tmp_path / "vector-miner"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "setup.sh", repo / "scripts" / "setup.sh")
    shutil.copy(ROOT / "pyproject.toml", repo / "pyproject.toml")
    for folder, tag in (leaves if leaves is not None else {str(LEAF): None}).items():
        (repo / folder / "example").mkdir(parents=True)
        dst = repo / folder / "example" / "Dockerfile"
        if tag is None:
            shutil.copy(ROOT / LEAF / "example" / "Dockerfile", dst)
        else:
            dst.write_text(f"FROM node:24-slim\nARG VECTOR_SIM_GIT=https://github.com/robotensor/vector-sim\n"
                           f"ARG VECTOR_SIM_REF={tag}\n")
    return repo


def _run(tmp_path: Path, repo: Path, rev: dict[str, str] | None = None, **extra: str):
    """Run setup.sh with fake git, uv and npm on PATH; returns (process, logged calls).

    A fake clone of vector-env gets a scripts/setup.sh that, like vector-env's, clones vector-sim beside it at
    VECTOR_SIM_REF. rev maps `git rev-parse` arguments to commits (unknown ones fail)."""
    bin_, log = tmp_path / "bin", tmp_path / "calls.log"
    bin_.mkdir(exist_ok=True)
    env_setup = ('cd "$(dirname "$0")/.."\n'
                 f'echo "env-setup VECTOR_SIM_REF=$VECTOR_SIM_REF VECTOR_ENV_JS_ONLY=$VECTOR_ENV_JS_ONLY" >> {log}\n'
                 'git clone --depth 1 --branch "$VECTOR_SIM_REF" '
                 '"${VECTOR_SIM_GIT:-https://github.com/robotensor/vector-sim}" ../vector-sim\n')
    (tmp_path / "env-setup.sh").write_text("#!/usr/bin/env bash\n" + env_setup)
    revs = "".join(f'  *" {k}") echo {v}; exit 0;;\n' for k, v in (rev or {}).items())
    _bin(bin_, "git", f'echo "git $*" >> {log}\n'
                      'if [ "$1" = clone ]; then d="${@: -1}"\n'
                      '  case "$d" in\n'
                      f'    *vector-env) mkdir -p "$d/vector_env" "$d/scripts"; cp {tmp_path}/env-setup.sh "$d/scripts/setup.sh"; '
                      'chmod +x "$d/scripts/setup.sh";;\n'
                      '    *vector-sim) mkdir -p "$d/js";;\n'
                      '  esac; exit 0; fi\n'
                      f'case "$*" in\n{revs}esac\nexit 1\n')
    _bin(bin_, "uv", f'echo "uv $*" >> {log}\n')
    _bin(bin_, "npm", f'echo "npm $* in $PWD" >> {log}\n')
    env = {k: v for k, v in os.environ.items()
           if k not in ("VECTOR_ENV_REF", "VECTOR_SIM_REF", "VECTOR_ENV_DIR", "VECTOR_ENV_GIT", "VECTOR_SIM_GIT",
                        "VECTOR_MINER_LEAF", "VECTOR_ENV_JS_ONLY")}
    env["PATH"] = f"{bin_}:{env['PATH']}"
    env.update(extra)
    p = subprocess.run(["bash", str(repo / "scripts" / "setup.sh")], env=env, capture_output=True, text=True,
                       timeout=60)
    calls = log.read_text().splitlines() if log.exists() else []
    return p, calls


def _clones(calls: list[str]) -> dict[str, str]:
    """{folder name: ref} of every `git clone --depth 1 --branch <ref> <url> <dir>`."""
    out = {}
    for c in calls:
        m = re.fullmatch(r"git clone --depth 1 --branch (\S+) (\S+) (\S+)", c)
        if m:
            out[Path(m.group(3)).name] = m.group(1)
    return out


def test_setup_pins_vector_env_and_vector_sim(tmp_path):
    tag = _round(ROOT / LEAF / "example" / "Dockerfile")
    assert re.fullmatch(r"bin-picking-expert-r[1-9][0-9]*", tag)
    repo = _repo(tmp_path)
    p, calls = _run(tmp_path, repo)
    assert p.returncode == 0, p.stderr
    assert calls[0] == f"git clone --depth 1 --branch {tag} https://github.com/robotensor/vector-env ../vector-env"
    assert _clones(calls) == {"vector-env": tag, "vector-sim": tag}
    assert f"env-setup VECTOR_SIM_REF={tag} VECTOR_ENV_JS_ONLY=1" in calls
    assert "uv pip install --python .venv/bin/python --require-hashes -r requirements.lock" in calls
    assert "uv pip install --python .venv/bin/python --no-deps -e ../vector-env -e ." in calls
    assert f"npm ci --no-audit --no-fund in {repo / LEAF / 'example'}" in calls
    assert (tmp_path / "vector-sim" / "js").is_dir()
    assert "assets: not installed yet" in p.stderr


def test_setup_refs_override_the_round_tag(tmp_path):
    repo = _repo(tmp_path)
    p, calls = _run(tmp_path, repo, VECTOR_ENV_REF="bin-picking-expert-r5", VECTOR_SIM_REF="bin-picking-expert-r6",
                    VECTOR_ENV_GIT="https://example.org/env", VECTOR_SIM_GIT="https://example.org/sim")
    assert p.returncode == 0, p.stderr
    assert _clones(calls) == {"vector-env": "bin-picking-expert-r5", "vector-sim": "bin-picking-expert-r6"}
    assert "git clone --depth 1 --branch bin-picking-expert-r5 https://example.org/env ../vector-env" in calls
    assert "git clone --depth 1 --branch bin-picking-expert-r6 https://example.org/sim ../vector-sim" in calls
    assert "env-setup VECTOR_SIM_REF=bin-picking-expert-r6 VECTOR_ENV_JS_ONLY=1" in calls


def test_setup_without_vector_sim_names_the_clone(tmp_path):
    repo = _repo(tmp_path, {str(LEAF): "bin-picking-expert-r3"})
    env_dir = tmp_path / "vector-env"
    (env_dir / "vector_env").mkdir(parents=True)
    (env_dir / "js" / "node_modules").mkdir(parents=True)
    p, calls = _run(tmp_path, repo)
    assert p.returncode == 0, p.stderr
    assert _clones(calls) == {} and not any(c.startswith("npm ") for c in calls)
    line = next(ln for ln in p.stderr.splitlines() if ln.startswith("example: no vector-sim"))
    assert "at bin-picking-expert-r3 (git clone --branch bin-picking-expert-r3 " in line
    assert f"(cd {LEAF}/example && npm ci)" in line


def test_setup_keeps_checkouts_and_warns_on_another_commit(tmp_path):
    repo = _repo(tmp_path, {str(LEAF): "bin-picking-expert-r2"})
    env_dir, sim_dir = tmp_path / "vector-env", tmp_path / "vector-sim"
    (env_dir / "vector_env").mkdir(parents=True)
    (env_dir / "js" / "node_modules").mkdir(parents=True)
    (sim_dir / "js").mkdir(parents=True)
    p, calls = _run(tmp_path, repo, rev={"HEAD": "a" * 40, "bin-picking-expert-r2^{commit}": "b" * 40})
    assert p.returncode == 0, p.stderr
    assert _clones(calls) == {} and not any(c.startswith("env-setup") for c in calls)
    assert "setup: warning: ../vector-env is at aaaaaaaaaaaa, not bin-picking-expert-r2" in p.stderr
    assert "(git -C ../vector-env checkout bin-picking-expert-r2)" in p.stderr
    assert "(git -C ../vector-sim checkout bin-picking-expert-r2)" in p.stderr
    assert (repo.parent / "vector-sim").is_dir()
    assert f"npm ci --no-audit --no-fund in {repo / LEAF / 'example'}" in calls


def test_setup_leaf_picks_the_folder(tmp_path):
    repo = _repo(tmp_path, {str(LEAF): "bin-picking-expert-r4", "skills/assembly/policy": "assembly-policy-r2"})
    p, calls = _run(tmp_path, repo, VECTOR_MINER_LEAF="assembly-policy")
    assert p.returncode == 0, p.stderr
    assert _clones(calls) == {"vector-env": "assembly-policy-r2", "vector-sim": "assembly-policy-r2"}
    assert f"npm ci --no-audit --no-fund in {repo / 'skills' / 'assembly' / 'policy' / 'example'}" in calls


def test_setup_leaf_without_its_folder_exits_1(tmp_path):
    repo = _repo(tmp_path)
    p, calls = _run(tmp_path, repo, VECTOR_MINER_LEAF="assembly-policy")
    assert p.returncode == 1 and calls == []
    assert p.stderr.strip() == ("setup: VECTOR_MINER_LEAF=assembly-policy: "
                                "no skills/assembly/policy/example/Dockerfile")


def test_setup_several_leaf_folders_need_the_leaf(tmp_path):
    repo = _repo(tmp_path, {str(LEAF): "bin-picking-expert-r4", "skills/assembly/policy": "assembly-policy-r2"})
    p, calls = _run(tmp_path, repo)
    assert p.returncode == 1 and calls == []
    assert p.stderr.count("\n") == 1
    assert "skills/assembly/policy" in p.stderr and "skills/bin-picking/expert" in p.stderr
    assert "VECTOR_MINER_LEAF" in p.stderr


def test_setup_no_leaf_folder_exits_1(tmp_path):
    repo = _repo(tmp_path, {})
    (repo / "skills" / "bin-picking" / "expert").mkdir(parents=True)
    p, calls = _run(tmp_path, repo)
    assert p.returncode == 1 and calls == []
    assert p.stderr.strip() == "setup: no skills/<skill>/<kind>/example/Dockerfile here"


def test_setup_dockerfile_without_round_tag_exits_1(tmp_path):
    repo = _repo(tmp_path, {str(LEAF): ""})
    (repo / LEAF / "example" / "Dockerfile").write_text("FROM node:24-slim\n")
    p, calls = _run(tmp_path, repo)
    assert p.returncode == 1 and calls == []
    assert f"no ARG VECTOR_SIM_REF=<tag> in {LEAF}/example/Dockerfile" in p.stderr
