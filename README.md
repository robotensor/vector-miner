# vector-miner

Miner tools for the Vector subnet's expert competition. You write an expert: a program that picks the target part
out of a bin, step by step, over the `vector-expert/2` protocol (see vector-env). Evaluation is continuous: you
commit one image, once per hotkey, and its evaluation starts as soon as your miner connects.

```mermaid
flowchart LR
  A["write your expert (example/)"] --> B["vector-miner eval: local score"]
  B --> DT["vector-miner eval --determinism N"]
  DT --> C["docker build, push to ghcr.io (public)"]
  C --> E["image@sha256 digest"]
  R["btcli subnet register: hotkey gets a uid"] --> F
  E --> F["vector-miner commit (once per hotkey, ever)"]
  F --> SV["vector-miner serve: your machine plays 1,000 fresh scenes (phase 1)"]
  SV --> SC["phase-1 score"]
  SC -->|"not above the king's best + margin"| BK["below_king (final)"]
  SC -->|"above"| Q["phase-2 queue, highest score first"]
  Q --> P2["phase 2: the subnet runs your image in docker on 100 of the scenes"]
  P2 -->|"an action hash differs, or a scene is over the wall"| DQ["disqualified"]
  P2 -->|"all equal"| KR["king run: the king's image on all 1,000 of your scenes"]
  KR -->|"you > king + 0.02"| CR["crowned"]
  KR -->|"else"| NC["not_crowned"]
  CR --> V["vector-miner verify --submission ID"]
  NC --> V
  DQ --> V
  BK --> V
```

On commit the subnet makes a secret salt for your submission and publishes its `salt_sha256`; the 1,000 scenes
are drawn from that salt. Phase 1 is played on your machine: `vector-miner serve` dials out to the subnet, our
simulator plays every scene against your image step by step, and each scene's rows are hashed (the action hash).
Phase 2 re-runs your committed image on our side (docker, `--network none`) on 100 of those scenes: every action
hash must equal phase 1's, or the submission is disqualified. Then the king's image plays all 1,000 of your scenes,
and the crown moves automatically when you beat the king by more than the margin (0.02) on those same scenes.

## Setup

```bash
git clone https://github.com/robotensor/vector-miner && cd vector-miner
scripts/setup.sh            # clones vector-env and vector-sim next to this folder if missing, .venv, npm ci
```

`setup.sh` clones vector-env at the tag of the version this package pins (`vector-env==0.1.0` in `pyproject.toml`:
`v0.1.0`) and vector-sim at `ARG VECTOR_SIM_REF=` of `example/Dockerfile` (`v0.1.0`), the simulator the example
image and the subnet's harness run. Checkouts already there are kept; one at another commit gets a warning with the
`git checkout` that fixes it. `VECTOR_ENV_REF` and `VECTOR_SIM_REF` override the pins (a tag, a branch or a sha).
`VECTOR_ENV_DIR=<folder>` uses (or clones) vector-env there instead of `../vector-env`. The example's
`@vector/sim` is `file:../../vector-sim/js`, so when vector-sim sits beside that other folder `setup.sh` links it to
`../vector-sim`.

Python 3.12 (`requires-python >=3.12`, `.python-version`), Node 22 or newer (`.nvmrc`: 24), git and docker.
`setup.sh` uses `uv` when it is installed, else `python3 -m venv`, and installs `requirements.lock` (every package
pinned with its hashes, the Bittensor SDK included) with `--require-hashes`. The commands then live in the project's
`.venv`: `.venv/bin/vector-miner`, `.venv/bin/vector-env` and `.venv/bin/btcli` (Bittensor CLI 11), or all three by
name after `source .venv/bin/activate`. Nothing needs a separate `pip install`.

### The asset bundle

Local evaluation needs the asset bundle (robot, crates, part library), pinned by version and sha256 in
vector-env's `vector_env/env.yaml`. Setup finishes without it while no download location is set. The location is
published with the subnet; then:

```bash
VECTOR_ENV_ASSETS_SOURCE=<asset-source> .venv/bin/vector-env assets pull    # downloads and checks the sha256
.venv/bin/vector-env assets info                                           # where the bundle is, its version
```

`VECTOR_ENV_BUNDLE=<folder>` uses a bundle you already have. Where the bundle is looked for, in order:
`VECTOR_ENV_BUNDLE` (or `VECTOR_SIM_BUNDLE`), `<vector-env>/bundle`, then `VECTOR_ENV_CACHE` (default
`~/.cache/vector-env/assets`)`/<version>`; `.venv/bin/vector-env assets dir` prints the folder found.

## From an expert to a submission

```bash
# 1. evaluate locally (a command while developing, then the image)
.venv/bin/vector-miner eval --command "node example/solve.mjs" --scenes 20 --seed 1
cd example && docker build -t ghcr.io/<you>/bin-expert:dev . && cd ..
.venv/bin/vector-miner eval --image ghcr.io/<you>/bin-expert:dev --scenes 100 --seed 7 --workers 4

# 2. check it is deterministic (phase 2 disqualifies an image that is not)
.venv/bin/vector-miner eval --image ghcr.io/<you>/bin-expert:dev --determinism 20 --seed 7 --workers 4

# 3. push to ghcr.io (a GitHub token with write:packages)
echo <github-token> | docker login ghcr.io -u <github-user> --password-stdin
docker push ghcr.io/<you>/bin-expert:dev

# 4. on GitHub: the package's settings -> change visibility -> public

# 5. the digest
docker inspect --format '{{index .RepoDigests 0}}' ghcr.io/<you>/bin-expert:dev    # ghcr.io/<you>/bin-expert@sha256:<digest>

# 6. a wallet, and its hotkey registered on the subnet (once; read "Wallet and registration" first)
.venv/bin/btcli wallet new-coldkey --wallet <coldkey>
.venv/bin/btcli wallet new-hotkey --wallet <coldkey> --wallet-hotkey <hotkey>
.venv/bin/btcli subnet burn-cost <netuid>
.venv/bin/btcli subnet register --netuid <netuid> --wallet <coldkey> --wallet-hotkey <hotkey>

# 7. optional: a trial commit on testnet (test TAO, never evaluated on mainnet)
.venv/bin/btcli subnet register --network test --netuid <test-netuid> --wallet <coldkey> --wallet-hotkey <hotkey>
.venv/bin/vector-miner commit --network test --netuid <test-netuid> --no-api-check \
  --image ghcr.io/<you>/bin-expert@sha256:<digest> --wallet.name <coldkey> --wallet.hotkey <hotkey>

# 8. commit, once: this hotkey can never commit another image
export VECTOR_MINER_API=https://vector.robotensor.ai
.venv/bin/vector-miner commit --image ghcr.io/<you>/bin-expert@sha256:<digest> --netuid <netuid> \
  --wallet.name <coldkey> --wallet.hotkey <hotkey>

# 9. serve it: phase 1 runs while this runs (start it within 48 h of the commit)
docker pull ghcr.io/<you>/bin-expert@sha256:<digest>
.venv/bin/vector-miner serve --image ghcr.io/<you>/bin-expert@sha256:<digest> \
  --wallet.name <coldkey> --wallet.hotkey <hotkey>

# 10. follow it
.venv/bin/vector-miner status --wallet.name <coldkey> --wallet.hotkey <hotkey>
```

`<netuid>` and `<test-netuid>` are published with the subnet.

### Wallet and registration

- `btcli` 11 (in `.venv`) names the wallet with `--wallet <coldkey>` and `--wallet-hotkey <hotkey>`;
  `vector-miner` takes `--wallet.name <coldkey>`, `--wallet.hotkey <hotkey>` and `--wallet.path`. Both read
  `~/.bittensor/wallets`. The coldkey holds your TAO and pays the registration: keep it encrypted and its recovery
  phrase offline. The hotkey signs your commit and every `serve` connection, and is what gets a uid on the subnet. It commits once:
  one submission per hotkey, ever.
- Registration costs TAO (burned, the price moves with demand: `subnet burn-cost` shows it, the confirmation prompt
  shows it again) or, when the subnet allows it, a proof of work (`subnet register --pow`). It is not refunded.
- A full subnet gives each new registration the uid with the lowest emission among those past their immunity period.
  Only the king earns, so a hotkey without the crown can lose its uid and must register (and pay) again. Check before
  the commit that it has one (`.venv/bin/btcli wallet overview --netuid <netuid> --wallet <coldkey>`): a
  commit from a hotkey without a uid is invalid.
- The testnet trial runs the same checks (without the API check) and sends the same signed call; `{"ok": true, ...}` there means the
  wallet, the reference and the setup work. Testnet registration uses test TAO, which has no value.

## Determinism

The same scene and the same messages must give the same rows, every run, on any machine. Phase 2 checks this on
100 of your scenes by comparing action hashes (vector-env's README defines the hash: FNV-1a 64 over every row value
you sent); one difference disqualifies the submission.

- Seed every random draw from the scene (`scene.scene_id`, `checkpoint`), never from the time, the process id or
  `/dev/urandom`. Python: `random.Random(scene_id)`, numpy: `np.random.default_rng(seed)`; JavaScript has no
  seeded `Math.random`: use a small seeded generator.
- Read no clock to decide anything: no time budgets ("plan until 1.5 s have passed"), no timeouts that change the
  plan. The simulator is paused while it waits for you; phase 2 runs on other hardware than yours.
- No threads or processes racing on a result: a parallel search must merge its results in a fixed order, and
  `--cpus` must not change the answer (phase 2 runs with `--cpus 4 --memory 8g`).
- Floating point: the same image gives the same floats; avoid reductions whose order depends on thread timing
  (multi-threaded BLAS, GPU kernels).
- `vector-miner eval --determinism N` runs N scenes, then runs them again in reverse order, and compares every
  action hash with vector-env's `harness.compare`. Exit 0 = all equal; 1 = a scene sent other rows (listed);
  2 = it could not tell (a scene did not run both times). Run it on the image itself, with `--workers` as high as
  phase 1 will run it.

## Phase 1: serve

```mermaid
sequenceDiagram
  participant M as vector-miner serve
  participant S as subnet API
  participant C as container per scene
  M->>S: GET /v1/expert/nonce
  S-->>M: {nonce}
  M->>S: WS /v1/expert/connect
  M->>S: hello {hotkey, image, nonce, signature}
  loop up to 8 scenes in flight
    S->>M: start {scene_key, ...vector-expert/2 start}
    M->>C: docker run --network none ... image (fresh)
    M->>C: start (without scene_key)
    C-->>M: act {rows}
    M->>S: act {scene_key, rows}
    S->>M: state {scene_key, ...}
    M->>C: state
    S->>M: end {scene_key, outcome}
    M->>C: end, then the container stops
  end
  S->>M: done
```

```mermaid
flowchart TD
  ST["serve starts"] --> W["start --warm containers (default --concurrency)"]
  W --> N["fetch a nonce"]
  N --> H["connect, send the signed hello"]
  H --> L["bridge start / act / state / end by scene_key"]
  L -->|"done"| X["exit 0"]
  L -->|"close 1008 or 4000-4999"| RF["refused: exit 1"]
  L -->|"other close, network error"| K["kill the running scenes' containers"]
  K --> B["wait 1 s, 2 s, 4 s ... 60 s (x 0.5-1)"]
  B --> N
  SIG["SIGINT / SIGTERM"] --> CL["close the socket (1000), kill every container: exit 0"]
```

- `serve --image REF` takes the image you committed (`ghcr.io/<you>/<repo>@sha256:<digest>`) and runs it as the
  subnet does in phase 2: one fresh container per scene, started by vector-env's own docker command
  (`harness.docker_argv`): `--network none`, read-only root filesystem, `/tmp` in memory, user 65534, no
  capabilities, 256 processes, `--cpus 4 --memory 8g`, the asset bundle at `/bundle` (`VECTOR_SIM_BUNDLE`). The
  image must be local (`--pull` pulls it). `--command "..."` runs a local program per scene instead (development,
  tests); the hello still names `--image`.
- The hello is signed by the wallet's hotkey: sr25519 over `vector-connect\n<image>\n<nonce>`, with a nonce fetched
  fresh for every connection. The subnet only plays scenes for the hotkey and image of a commit.
- Your expert sees exactly the `vector-expert/2` lines of a local `eval` (the `scene_key` is removed); what it
  writes goes back with its `scene_key`. A line that is not a JSON object, a line over 1 MiB, or an exit before the
  scene's `end` is sent to the subnet as `{"type":"error"}` for that scene.
- The subnet waits 2 s for each `act` (the simulator is paused meanwhile); a scene has a wall cap, and phase 1 a
  total cap. A timeout or a dropped connection is your failure. Containers are started ahead (`--warm N`, default
  `--concurrency`) so a scene does not wait for `docker run`; `--concurrency N` (default 8, what the subnet sends at
  once) bounds the scenes running. Keep it at 8: a scene that waits for a free slot spends its 2 s.
- When the connection drops, the running scenes' containers are killed and `serve` reconnects with backoff
  (1 s doubling to 60 s, times 0.5 to 1); the subnet restarts the unfinished scenes from tick 0, at most twice per
  scene. Keep `serve` running until it prints `done`.
- `{"type":"welcome"}` and `{"type":"error"}` (without a `scene_key`) from the subnet are printed; `{"type":"done"}`
  (phase 1 finished) ends `serve` with exit 0. A close with code 1008 or 4000-4999 means the subnet refused the
  miner (bad signature, no such commit, another connection already serving it): exit 1 with the reason, no retry.
- Ctrl-C or SIGTERM closes the socket (code 1000) and kills every container: exit 0. `--stderr DIR` keeps each
  container's stderr in `DIR/<container name>.log`; the log names the scene of each container.

## Status

```bash
.venv/bin/vector-miner status --api https://vector.robotensor.ai --wallet.name <coldkey> --wallet.hotkey <hotkey>
```

One JSON object: `king` (whether you are the king), `king_hotkey`, `king_image` (null for the genesis king, which
runs as a command: `king_note` then says `king: no image published`), `king_best`, `margin`, and `submissions`, each
with `id`, `image`, `status`, `committed_at`, `salt_sha256`, `phase1 {done, total, score, successes}`,
`queue_position` (phase-2 queue, highest phase-1 score first), `phase2`, `king_same_scenes {hotkey, image, done,
total, score}` (the king's run on your scenes), `reason`, `final` and `verdict` (one line, also printed to stderr).
It reads `GET /v1/skills/bin-picking/expert` and `GET /v1/skills/bin-picking/expert/submissions?hotkey=<ss58>`.

| status | meaning |
|---|---|
| `waiting_connection` | committed; run `serve` (abandoned after 48 h without a connection) |
| `phase1` | `serve` is playing your scenes: `phase1.done` of `phase1.total` |
| `scored` | phase 1 is done and above the king's best + margin: in the phase-2 queue (`queue_position`) |
| `below_king` | phase 1 is not above the king's best + margin; final. A queued submission is re-checked after every crowning |
| `phase2` | we run your image in docker on 100 of your scenes and compare action hashes |
| `king_run` | phase 2 passed; the king's image plays all your scenes |
| `crowned` / `not_crowned` | your phase-1 score against the king's on the same scenes, with the margin; final |
| `disqualified` | phase 2 found another action hash or a scene over the wall (`reason`); final |
| `duplicate_hotkey` | a second commit from a hotkey that already had one; never evaluated |
| `invalid` / `copy` | a bad commit, the digest of an earlier submission; neither uses the hotkey's submission, so it may commit again |
| `abandoned` | no connection within 48 h; final, and it used the hotkey's submission |

## Commands

```bash
vector-miner eval (--command "..." | --image REF) [--scenes 20 | --determinism N] [--seed 0] [--workers 1]
                  [--time-limit 15] [--wall 45] [--cpus 4] [--memory 8g] [--docker docker] [--pull] [--results FILE]
                  [--stderr DIR] [--trace DIR] [--scene-cache DIR | --no-scene-cache]
vector-miner commit --image REF --netuid <netuid> [--network finney] [--api URL | --no-api-check]
                    [--wallet.name --wallet.hotkey --wallet.path] [--no-check]
vector-miner serve --image REF [--api URL] [--concurrency 8] [--warm N] [--command "..."] [--cpus 4] [--memory 8g]
                   [--docker docker] [--pull] [--stderr DIR] [--wallet.name --wallet.hotkey --wallet.path]
vector-miner status [--api URL] [--hotkey <ss58>] [--wallet.name --wallet.hotkey]
vector-miner verify --submission ID (--api URL | --doc FILE) [--save FILE] [--hash-scenes M]
                    [--image REF|submission|king [--command "..."] [--scenes 20] [--sample-seed 0] [--workers 1]
                    [--wall S] [--cpus 4] [--memory 8g] [--docker docker] [--pull]] [--salt-sha256 HEX]
                    [--allow-uncommitted]
vector-miner verify --round N ...            # a weekly round from before v8 (same options)
```

`$VECTOR_MINER_API` can hold the API address (`--api`).

- `eval` generates random scenes from `--seed` and runs your expert through the same harness the subnet uses.
  `--command` runs a local program (no docker, no sandbox). `--image` starts a fresh container per scene as
  phase 2 does: `--network none`, read-only root filesystem, `/tmp` in memory (1 GB), user 65534 with `HOME=/tmp`,
  no capabilities, 256 processes, 1024 open files, `--cpus 4 --memory 8g`, 15 s of robot time and 45 s of wall
  time per scene. The image must already be local (`--pull` pulls it first). `--results` writes one JSON line per
  scene (both runs with `--determinism`); `--stderr DIR` writes your expert's stderr of every scene to
  `DIR/<index, 4 digits>-<scene_id>.log` (e.g. `0000-s121011998878783-4a5b69a2.log`); `--trace DIR` writes a
  per-tick trace of every scene (below). Change the limits only to experiment: the subnet uses the defaults.
  Without an asset bundle (see vector-env) `eval` prints the reason and exits 2.
- Generating a scene takes about 2 s. `--workers N` generates (and runs) N scenes at a time, and generated scenes
  are kept in `~/.cache/vector-env/scenes` (per vector-env version, asset sha256, draw, seed and index), so the
  next `eval` with the same `--seed` reuses them: compare variants of your expert on the same seed.
  `--scene-cache DIR` (or `VECTOR_ENV_SCENE_CACHE`) moves the cache, `--no-scene-cache` turns it off.
- `commit` applies the subnet's checks before it sends anything: the reference is
  `<registry>/<repo>@sha256:<64 hex>` (lower case, no tag), at most 128 characters, and the registry is `ghcr.io`.
  It then asks the registry anonymously for that digest, as the subnet does: a private image, an unknown digest or
  a registry that answers another digest is refused (`--no-check` skips this). The reference is committed without
  surrounding spaces.
- One submission per hotkey, ever. `commit` asks the public API
  (`GET /v1/skills/bin-picking/expert/submissions?hotkey=<ss58>`) first and refuses when the hotkey's submission is
  used: any earlier commit except an `invalid`, `copy` or `duplicate_hotkey` one, which the subnet does not count.
  The subnet would record a second commit as `duplicate_hotkey` and never evaluate it. To submit another image, register another hotkey. When the API cannot be reached `commit` stops;
  `--no-api-check` skips the check (the testnet trial). The same digest as an earlier submission is a `copy`.
- After a commit, `commit` prints the `serve` command to start; phase 1 waits for it at most 48 h.

### Inspect a failure

```bash
.venv/bin/vector-miner eval --command "node example/solve.mjs" --scenes 30 --seed 1 --workers 4 \
  --results r.jsonl --trace traces --stderr logs
#   why s27260578352828-f7734b96: timed_out at tick 300 (15 s): 15 s | ... | the target was held for 18 ticks
#   (ticks 52-69); it was not held from tick 70 | its lowest point got to 202.3 mm below the lift line ...
.venv/bin/vector-env scene --seed 27260578352828 > scene.json
.venv/bin/vector-env replay --scene scene.json --result r.jsonl --trace one.json
```

Each failed scene gets a `why` line. `traces/<index>-<scene_id>.json` holds, per tick, the row stepped, the joints,
the grip, the tcp pose, the parts held and touched and the target's height against the lift line, plus `reasons`
and a `summary`; vector-env's README lists every field. The scene's seed is the number after `s` in its id.

## Verify a submission

Once a submission is final (`below_king`, `crowned`, `not_crowned` or `disqualified`), the API publishes everything
needed to check it at `GET /v1/skills/bin-picking/expert/submissions/<id>/verify` (404 until then): the salt and
its commitment, the vector-env version and asset sha256 the scenes were made with, every scene's seed and state
hash, phase 1 (score and per-scene success, time, outcome, action hash), phase 2 (the scenes, their action hashes
and wall times), the king's run on the same scenes and the decision.

```mermaid
flowchart LR
  CM["salt_sha256 (shown from the commit on)"] --> CK{"sha256(salt) = commitment, committed before phase 1?"}
  S["salt (published once final)"] --> CK
  S --> SD["seed i = sha256(salt:0:i)[:6] + k"]
  SD --> G["vector-env: generate the scene"]
  G --> H{"seed and state hash = published?"}
  D["published phase 1, phase 2, king run"] --> C{"scores, phase-2 hashes, gate, crown add up?"}
  G --> R["--image: run an image on K sampled scenes"]
  R --> A{"action hash = published?"}
```

```bash
# check the document and regenerate all 1000 scenes (about 2 s each; --hash-scenes 100 checks a sample)
.venv/bin/vector-miner verify --submission <id> --api https://vector.robotensor.ai

# also re-run the submission's image on 20 sampled scenes in docker and compare action hashes
.venv/bin/vector-miner verify --submission <id> --api https://vector.robotensor.ai --hash-scenes 100 \
  --image submission --scenes 20 --pull

# the king's image on the same scenes; the salt commitment you noted at commit time
.venv/bin/vector-miner verify --submission <id> --api https://vector.robotensor.ai --image king --scenes 10 \
  --salt-sha256 <64 hex>
```

- Seeds: scene `i` uses `int.from_bytes(sha256(f"{salt}:0:{i}").digest()[:6], "big")`, vector-env's
  `scenes.seed_for(0, i, salt)`, plus `k`: the first `k` below `seed_tries` (6) whose draw places a scene
  (`scenes.draw_scene`). An index none of whose draws places a scene is listed in `skipped`; a full check also
  confirms every skipped index fails here.
- The salt: `salt_sha256` must be the sha256 of the published salt, committed (`salt_committed_at`) no later than
  `phase1_started_at`, and equal to `--salt-sha256` when given, else to the submission view's `salt_sha256`
  (`GET /v1/skills/bin-picking/expert/submissions/<id>`) when the document comes from `--api`. A null
  `salt_committed_at` (or `phase1_started_at`) is a document problem (exit 1); `--allow-uncommitted` lists it under
  `document.warnings` instead and checks the rest.
- The document check: the seeds derive from the salt, `scenes_sha256` is
  `sha256("\n".join(f"{index}:{seed}:{state_hash}"))`, the phase-1 score is successes / scenes with a verdict
  (infrastructure failures, an `INFRA_OUTCOMES` outcome or none, are left out; `not_run` and timeouts count as
  failures), mean times match the successful scenes; `below_king` needs a phase-1 score not above
  `decision.king_best` + `margin` and no phase 2; any other final status needs one above it and a phase 2 on the
  sample rule's scenes (the smallest `sha256(f"{salt}:phase2:{index}")` among the phase-1 verdicts with an action
  hash), each with an outcome, an action hash and a wall time; phase-2 infrastructure failures are left out of the
  comparison, and it passes when at least one scene is compared, every hash equals phase 1's and no scene is a
  `wall_timeout` or over `wall_s`; `disqualified` needs a phase 2 that did not pass and a reason; `crowned` and
  `not_crowned` need phase 2 passed and a king run, whose score and `decision.score` are successes on the scenes
  where both runs gave a verdict, and `crowned` exactly when the submission's is above the king's + `margin` there.
  Images must be `ghcr.io/<repo>@sha256:<digest>` with their own `digest`; the genesis king (no `id`) runs as a
  command and has no image: a warning `king: no image published`, and `--image king` skips the re-run.
- `--image` takes `submission`, `king`, or that image's reference, digest or hotkey; it runs the image (or
  `--command` instead) on `--scenes` scenes with a published verdict, sampled with `--sample-seed`, and reports
  `hash_equal`, `hash_agreement`, `outcome_agreement` and every scene whose action hash differs. A deterministic
  image gives the published hashes on any machine. Re-run differences do not change the exit code.
- `phase2` in the output repeats the phase-2 comparison: `compared`, `equal`, `mismatched`, `over_wall`, `infra`,
  `passed`; `same_scenes` repeats the decision: `compared`, `score`, `king_score`.
- Regeneration, `--save`, `--doc`, strict JSON, the size limit and the exit codes are as for a round, below; a
  `--doc` of another submission, an image that did not run for the submission, or giving both or neither of
  `--submission` and `--round` exit 2.

## Verify a weekly round (before v8)

Nobody sees a round's scenes before the evaluation: they come from a salt drawn while the round is open and kept
secret; its `salt_sha256` is shown on the board and in the round view from then on, so the salt cannot be changed
later to pick favourable scenes. Once the round is final (evaluated, and the crown kept or moved), the API publishes everything needed to check it at
`GET /v1/skills/bin-picking/expert/rounds/<n>/verify` (404 until then): the salt and its commitment, the vector-env version and asset
sha256 the scenes were made with, every scene's seed and state hash, and every evaluated image (the king's re-run
and each challenger, by digest) with its score, rank and per-scene outcome and time.

```mermaid
flowchart LR
  CM["salt_sha256 (shown while open)"] --> CK{"sha256(salt) = commitment?"}
  S["salt (published after the round is final)"] --> CK
  S --> SD["seed i = sha256(salt:n:i)[:6] + k"]
  SD --> G["vector-env: generate the scene"]
  G --> H{"seed and state hash = published?"}
  D["published results"] --> C{"scores, ranks, crown add up?"}
  G --> R["--image: run the image on K sampled scenes"]
  R --> A["agreement with the published outcomes"]
```

```bash
.venv/bin/vector-miner verify --round 12 --api https://vector.robotensor.ai

# also re-run the winner on 20 sampled scenes in docker and compare outcomes
.venv/bin/vector-miner verify --round 12 --api https://vector.robotensor.ai --hash-scenes 100 \
  --image ghcr.io/<them>/bin-expert@sha256:<digest> --scenes 20 --pull

# also compare the salt with the commitment you noted from the board while the round was open
.venv/bin/vector-miner verify --round 12 --api https://vector.robotensor.ai --salt-sha256 <64 hex>

# your own program in place of an image (no docker)
.venv/bin/vector-miner verify --round 12 --api https://vector.robotensor.ai --image <digest> \
  --command "node example/solve.mjs" --scenes 5
```

- Seeds: scene `i` of round `n` uses `int.from_bytes(sha256(f"{salt}:{n}:{i}").digest()[:6], "big")`, which is
  vector-env's `scenes.seed_for(n, i, salt)`, plus `k`: the first `k` below `seed_tries` (6) whose draw places a
  scene. An index none of whose draws places a scene is listed in `skipped`; a full check (no `--hash-scenes`)
  also confirms every skipped index fails here. `draw`, `task` and `wall_s` are the round's own settings.
- The salt: `salt_sha256` must be the sha256 of the published salt, committed (`salt_committed_at`) no later than
  the close (`closed_at`), and equal to `--salt-sha256` when given, else to the round view's `salt_sha256` when
  the document comes from `--api`. A null `salt_committed_at` (or `closed_at`) means the document cannot show the
  salt was fixed before the close: that is a document problem (exit 1). `--allow-uncommitted` lists it under
  `document.warnings` instead and checks the rest.
- The document check: the seeds derive from the salt, `scenes_sha256` is
  `sha256("\n".join(f"{index}:{seed}:{state_hash}"))`, every score is successes / scenes, mean times match the
  successful scenes, ranks follow score, then mean time, then commit block, and a crown only went to a challenger
  that beat the king's re-run by more than the margin; a decision that names a king needs that king's image, and
  the winner is the best-ranked challenger that is not `infra_failed`. Each evaluated `run_image` must be
  `ghcr.io/<repo>@sha256:<digest>` with the image's own `digest`; otherwise it is a document problem and `--image`
  does not run (or pull) it.
- Images that did not finish are listed under `unfinished`: status `stopped` or `infra_failed`, `stop_reason`,
  how many scenes are `not_run`, `infra_failures`. A re-run skips the image's `not_run` scenes.
- An image without a published `run_image` is listed under `not_rerunnable`; the genesis king (the team's expert,
  run as a command, `team: true`) shows as "team expert (not publicly re-runnable)". Its scenes, scores and the
  decision are still checked; `--image` on it needs `--command`.
- The document is read as strict JSON (no NaN), at most 64 MB, and every field is type-checked first.
- Regeneration needs the same vector-env version and asset bundle as the round (`env` in the document, or a
  scene's own `env` when it was made with another one); with another one, the output warns, and a scene whose
  hash differs is listed under `unverified` (not as a mismatch). When the generator fails on every scene, including
  a known published one (no node, a missing or broken asset bundle), the check stops with exit 2.
- `--image` takes an evaluated image's reference, digest or hotkey. It runs that image's published `run_image` (the
  reference the round ran) on `--scenes` scenes sampled with `--sample-seed`, or `--command` instead, and reports
  `agreement` (same success), `outcome_agreement`, the time difference and every disagreeing scene. A different
  machine can change a few borderline outcomes; the published results are what the round decided on.
  `--wall` defaults to the round's `wall_s` (else vector-env's `harness.wall_s`).
- `--save FILE` keeps the document; `--doc FILE` checks a saved one offline. The output is one JSON object.
  Exit codes: 0 = the document adds up and every regenerated scene matches; 1 = a document problem (a round
  without a salt commitment time included, unless `--allow-uncommitted`) or a scene mismatch (listed in the output); 2 = cannot check: a bad argument (`--command` without `--image`, no `--api`
  without `--doc`, a `--doc` that is unreadable or of another round, `--workers` below 1, a malformed
  `--salt-sha256`), a malformed, non-JSON or oversized document, the round is not final (404) or the API failed,
  the image was not evaluated in the round, vector-env is missing or its generator does not work here, the pull
  failed or the re-run could not run (docker missing, every scene an infra outcome), or scenes are `unverified`
  (the output is printed). Re-run disagreements are reported under `rerun` and do not change the exit code.

The example image (`example/Dockerfile`) runs as user 65534 with `HOME=/tmp` and writes nothing outside `/tmp`.
Files your image reads must be readable by that user.

## Tests

```bash
.venv/bin/python -m pytest
```

The `serve` tests run a fake subnet (a `websockets` server with the nonce endpoint) and `tests/fake_expert.py`;
with an asset bundle they also play real scenes through vector-env's remote harness and compare the action hashes
with a local run. No test talks to a chain.

## License

Apache-2.0.
