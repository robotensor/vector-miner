# vector-miner spec

vector-miner is the miner kit of the Vector subnet: one `vector-miner` command, with the commands of each
competition (leaf) in its own folder. This file covers the repository and the shared parts; each leaf has its own
spec (`skills/<skill>/<kind>/spec.md`).

## 1. Layout

| path | what |
|---|---|
| `vector_miner/__init__.py` | `__version__` (unchanged) |
| `vector_miner/cli.py` | the `vector-miner` command (section 3) |
| `vector_miner/wallet.py` | wallet flags and keys (section 4) |
| `vector_miner/api.py` | the HTTP client (section 4) |
| `vector_miner/protocol.py` | image references, request signatures, the connect hello (unchanged) |
| `vector_miner/chain.py` | registry checks, on-chain commit, transfers, commitment reads (unchanged) |
| `vector_miner/skills/__init__.py` | the leaf registry (section 2) |
| `vector_miner/skills/<skill_py>/<kind>/` | one leaf's commands |
| old module paths in `vector_miner/` | section 5 |
| `skills/<skill>/<kind>/` | one leaf's `README.md`, `spec.md`, `example/`, `base/`, `tests/` |
| `tests/` | shared tests (protocol, chain, command, old paths, `setup.sh`) |
| `scripts/setup.sh`, `pyproject.toml`, `requirements.in`, `requirements.lock`, `README.md`, `spec.md` | the repository |

Leaves: `bin-picking-expert` (`vector_miner/skills/bin_picking/expert/`, `skills/bin-picking/expert/`).

Shared code names no skill or leaf. The only shared files that name one are the old paths of section 5 and their
tests.

## 2. Leaf registry

```python
vector_miner.skills.leaves() -> dict[str, types.ModuleType]   # {leaf id: leaf package}, sorted by id
vector_miner.skills.leaf(leaf_id: str) -> types.ModuleType    # KeyError("unknown leaf X (installed: a, b)")
vector_miner.skills.default() -> str | None                   # $VECTOR_MINER_LEAF, else the only leaf, else None
```

- A leaf is a package `vector_miner.skills.<skill_py>.<kind>` whose `__init__` defines `ID`, `SKILL` and `KIND`
  with `ID == f"{SKILL}-{KIND}"`, `SKILL.replace("-", "_") == <skill_py>`, `KIND == <kind>` and `KIND` one of
  `expert`, `policy`, `contribution`. Other packages are not leaves.
- A leaf package's `__init__` imports nothing else.
- Each leaf has a module `cli` with `main(argv: list[str] | None = None, prog: str = "vector-miner") -> int`.
- Adding a leaf is adding its folders; shared code does not change.

## 3. The `vector-miner` command

Entry point unchanged: `vector-miner = "vector_miner.cli:main"`.

```
vector-miner [--leaf ID] <command> [args...]
vector-miner leaves
```

- `--leaf ID` or `--leaf=ID` is read only before the command. Without it the leaf is
  `vector_miner.skills.default()`. `--leaf` without a value exits 2 with `vector-miner: error: argument --leaf:
  expected one argument` (after the usage).
- `leaves` prints one JSON line `{"leaves": [{"id", "skill", "kind"}, ...], "default": <id or null>}` and exits 0.
- An unknown leaf exits 2 with `vector-miner: unknown leaf X (installed: a, b)`. No leaf chosen while several are
  installed exits 2 with `vector-miner: several leaves are installed (a, b): give --leaf ID or set
  VECTOR_MINER_LEAF`; no leaf installed at all exits 2 with `vector-miner: no leaf is installed`.
- Otherwise it returns `leaf(id).cli.main(rest, prog="vector-miner")`: commands, flags, output and exit codes are
  the leaf's. With one leaf installed every earlier command line works unchanged.
- `-h` / `--help` with no command shows the default leaf's help when there is one; with no default it prints
  `usage: vector-miner [--leaf ID] <command> [args...]` / `vector-miner leaves`, the installed leaves, and exits 0.

## 4. Shared modules

`vector_miner.wallet`:

```python
DEFAULT_WALLETS: str                                   # ~/.bittensor/wallets, expanded
common() -> argparse.ArgumentParser                    # add_help=False parent: --wallet.name, --wallet.hotkey, --wallet.path
btcli_create(name: str, hotkey: str, path: str) -> str # the btcli commands that create the wallet (unchanged text)
keypair(a, alt: str = "")                              # the wallet's hotkey keypair, else SystemExit with the same message as before
wallet_flags(a) -> str                                 # the --wallet.* flags that differ from the defaults
coldkey_address(a) -> str | None                       # the wallet's coldkey ss58, None when it cannot be read
```

`vector_miner.api`:

```python
MAX_BYTES = 64 << 20
class ApiError(Exception): status: int; detail: str    # str(e) == f"HTTP {status}: {detail}"
make_client(timeout: float) -> httpx.Client            # looked up at call time (tests replace it)
class Api:
    def __init__(self, base: str, keypair=None, client=None, timeout: float = 60.0)
    def get(self, path: str, max_bytes: int = MAX_BYTES) -> dict
    def get_bytes(self, path: str, max_bytes: int) -> bytes
    def post_signed(self, path: str, obj: dict | None = None) -> dict
```

Leaf API paths live in the leaf (its `api.py`). `protocol.py` and `chain.py` keep every name and behaviour.

## 5. Old module paths

The old paths of this section are temporary. They stay through round `bin-picking-expert-r2` and are removed in the first change after that tag.

`vector_miner.board`, `.local`, `.serve`, `.subverify` and `.verify` stay, each the same module object as
`vector_miner.skills.bin_picking.expert.<same name>`. Each is a module that imports the new one and replaces itself
in `sys.modules`:

```python
"""Old import path of vector_miner.skills.bin_picking.expert.subverify."""
import sys

from .skills.bin_picking.expert import subverify as _module


def __getattr__(name):
    return getattr(_module, name)


sys.modules[__name__] = _module
```

The module-level `__getattr__` makes a name imported from the old path resolve even when several threads import
it for the first time at once (a thread may then hold the old module object instead of the new one).

## 6. `scripts/setup.sh`

- The leaf folder is `skills/<skill>/<kind>/` of `VECTOR_MINER_LEAF` (the id split at its last `-`), else the
  only `skills/*/*/` with `example/Dockerfile`; none or several (without `VECTOR_MINER_LEAF`) exits 1 with one line
  naming the folders found.
- The round tag is `ARG VECTOR_SIM_REF=` of `<leaf folder>/example/Dockerfile`. vector-sim is fetched at
  `VECTOR_SIM_REF`, default the round tag; vector-env at `VECTOR_ENV_REF`, default the round tag.
  `VECTOR_ENV_DIR` (default `../vector-env`), `VECTOR_ENV_GIT` and `VECTOR_SIM_GIT` as before.
- A checkout already there is kept; one at another commit gets a warning with the `git checkout` that fixes it.
- Then, as before: vector-env's JS setup when its `js/node_modules` is missing (`VECTOR_ENV_JS_ONLY=1
  VECTOR_SIM_REF=<ref> <vector-env>/scripts/setup.sh`); `.venv` with `uv` when installed, else `python3 -m venv`;
  `requirements.lock` with `--require-hashes`; editable installs of vector-env and this checkout with `--no-deps`;
  `../vector-sim` linked to vector-env's sibling when missing; `npm ci` in `<leaf folder>/example`;
  `vector-env assets pull`, which may fail with one line saying how to get the bundle.
- Messages name the new paths (`<leaf folder>/example/Dockerfile`, the example's `@vector/sim` path).

## 7. Packaging and tests

- `[tool.setuptools.packages.find] include = ["vector_miner*"]` (finds the leaf packages).
- `[tool.pytest.ini_options]`: `testpaths = ["tests", "skills"]`, `norecursedirs = ["node_modules", ".*",
  "*.egg-info", "example", "base"]`.
- `tests/` has no `conftest.py`; each leaf's `skills/<skill>/<kind>/tests/` has its own. Test module names are
  unique across the repository. Test modules import their `conftest` only at module level.

```bash
.venv/bin/python -m pytest
```

## 8. Rounds

What miners run for a round is the git tag `<leaf-id>-r<n>` in vector-miner, vector-env and vector-sim. Round 1 of
`bin-picking-expert` is `bin-picking-expert-r1`. The READMEs tell miners to clone vector-miner, vector-env and
vector-sim side by side at the tag (`git clone --branch bin-picking-expert-r1 https://github.com/robotensor/<repo>`),
then run `scripts/setup.sh`, which keeps those checkouts. No `v<number>` tag is used as a pin.

## 9. Compatibility

- Every command line, output and exit code of the earlier `vector-miner` stays.
- Leaf code imports vector-env only by the module names `vector_env.assets`, `.cli`, `.compose`, `.harness`,
  `.node`, `.rules`, `.scenes` and `.settings`, which exist at every round tag of vector-env.
