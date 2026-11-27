# vector-miner

The miner kit of the Vector subnet: one `vector-miner` command, and one folder per competition (a leaf of the
skill tree) with its guide, its example, its base image and its tests.

## Leaves

| leaf | guide | what you write |
|---|---|---|
| `bin-picking-expert` | [skills/bin-picking/expert/README.md](skills/bin-picking/expert/README.md) | an expert that picks the target part out of a bin |

A leaf's folder is `skills/<skill>/<kind>/`; its id is `<skill>-<kind>`. Each leaf has its own rounds, settings
and emission; an expert or policy leaf also has its own submission fee.

## Rounds and pins

Each leaf runs in rounds of its own. What you run for a round is the git tag `<leaf id>-r<n>` in vector-miner,
vector-env and vector-sim. Round 2 of `bin-picking-expert` is `bin-picking-expert-r2`. Clone the three repositories
side by side at the tag of the round you play, then run setup:

```bash
for repo in vector-miner vector-env vector-sim; do
  git clone --branch bin-picking-expert-r2 https://github.com/robotensor/$repo
done
cd vector-miner && scripts/setup.sh
```

`setup.sh` keeps the vector-env and vector-sim checkouts beside it.

Round 1 was `bin-picking-expert-r1` of vector-miner; it installed vector-env and vector-sim at `v0.1.0`.

## Setup

Python 3.12 (`requires-python >=3.12`, `.python-version`), Node 22 or newer (`.nvmrc`: 24), git and docker.

`scripts/setup.sh`:

- Picks the leaf folder: `skills/<skill>/<kind>/` of `VECTOR_MINER_LEAF` (a leaf id), else the only folder with an
  `example/Dockerfile`. With none, or several and no `VECTOR_MINER_LEAF`, it exits 1 with one line naming the
  folders it found (`VECTOR_MINER_LEAF=<leaf id> scripts/setup.sh` picks one).
- Reads the round tag from `ARG VECTOR_SIM_REF=` in that folder's `example/Dockerfile`, and clones vector-env and
  vector-sim beside this folder at that tag when they are missing (`../vector-env`, `../vector-sim`).
  `VECTOR_ENV_REF` and `VECTOR_SIM_REF` override the tag; `VECTOR_ENV_GIT` and `VECTOR_SIM_GIT` name another
  source; `VECTOR_ENV_DIR=<folder>` uses (or clones) vector-env
  there instead of `../vector-env`, and links `../vector-sim` to the vector-sim beside it.
  A checkout already there is kept; one at another commit gets a warning with the `git checkout` that fixes it.
- Creates `.venv` (with `uv` when it is installed, else `python3 -m venv`) and installs `requirements.lock`, every
  package pinned with its hashes (the Bittensor SDK included), with `--require-hashes`, then vector-env and this
  checkout.
- Runs `npm ci` in the leaf's `example/` (its `@vector/sim` is the vector-sim beside this folder).
- Pulls the asset bundle (below), when its location is set.

The commands then live in `.venv`: `.venv/bin/vector-miner`, `.venv/bin/vector-env` and `.venv/bin/btcli`
(Bittensor CLI 11), or all three by name after `source .venv/bin/activate`. Nothing needs a separate `pip install`.

### The asset bundle

Local evaluation needs the asset bundle (robot, cell, part library), pinned by version and sha256 in vector-env.
Setup finishes without it while no download location is set. The location is
published with the subnet; then:

```bash
VECTOR_ENV_ASSETS_SOURCE=<asset-source> .venv/bin/vector-env assets pull    # downloads and checks the sha256
.venv/bin/vector-env assets info                                           # where the bundle is, its version
```

`VECTOR_ENV_BUNDLE=<folder>` uses a bundle you already have. The bundle is looked for, in order, in
`VECTOR_ENV_BUNDLE` (or `VECTOR_SIM_BUNDLE`), `<vector-env>/bundle`, then `VECTOR_ENV_CACHE` (default
`~/.cache/vector-env/assets`)`/<version>`; `.venv/bin/vector-env assets dir` prints the folder found.

## Wallet and registration

```bash
.venv/bin/btcli wallet new-coldkey --wallet <coldkey>
.venv/bin/btcli wallet new-hotkey --wallet <coldkey> --wallet-hotkey <hotkey>
.venv/bin/btcli subnet burn-cost <netuid>
.venv/bin/btcli subnet register --netuid <netuid> --wallet <coldkey> --wallet-hotkey <hotkey>
.venv/bin/btcli wallet overview --netuid <netuid> --wallet <coldkey>     # the hotkey's uid
```

- `btcli` 11 names the wallet with `--wallet <coldkey>` and `--wallet-hotkey <hotkey>`; `vector-miner` takes
  `--wallet.name <coldkey>`, `--wallet.hotkey <hotkey>` and `--wallet.path`. Both read `~/.bittensor/wallets`.
- The coldkey holds your TAO, pays the registration and pays a leaf's submission fee. Keep it encrypted and its recovery
  phrase offline. The hotkey signs your commits and connections, and is what gets a uid on the subnet.
- Registration costs TAO (burned; the price moves with demand: `subnet burn-cost` shows it, the confirmation prompt
  shows it again) or, when the subnet allows it, a proof of work (`subnet register --pow`). It is not refunded.
- A full subnet gives each new registration the uid with the lowest emission among those past their immunity period.
  A hotkey that earns nothing can lose its uid and must register (and pay) again. A commit from a hotkey without a
  uid is invalid.
- Testnet trial: register on testnet (test TAO, which has no value) and commit there with `--network test
  --netuid <test-netuid> --no-api-check`. It runs the same local checks and sends the same signed call;
  `{"ok": true, ...}` means the wallet, the image reference and the setup work. Nothing on testnet is evaluated on
  mainnet.

`<netuid>` and `<test-netuid>` are published with the subnet.

## The command

```
vector-miner [--leaf ID] <command> [args...]
vector-miner leaves
```

- `vector-miner leaves` prints one JSON line: `{"leaves": [{"id", "skill", "kind"}, ...], "default": <id or null>}`.
- `--leaf ID` (or `--leaf=ID`, given before the command) picks the leaf; without it the leaf is
  `VECTOR_MINER_LEAF`, else the only one installed. With one leaf installed every command works without `--leaf`.
- The commands, their flags, output and exit codes are the leaf's: its guide lists them, and
  `vector-miner [--leaf ID] <command> -h` shows them.
- An unknown id exits 2 with `vector-miner: unknown leaf X (installed: ...)`. Several leaves and none chosen exits 2
  and asks for `--leaf ID` or `VECTOR_MINER_LEAF`; `vector-miner -h` then prints the usage and the installed leaves.
- `$VECTOR_MINER_API` can hold the API address (`--api`).

## Tests

```bash
.venv/bin/python -m pytest
```

It runs `tests/` (the shared code, `setup.sh`, the command) and every `skills/<skill>/<kind>/tests/`. No test talks
to a chain.

## License

Apache-2.0.
