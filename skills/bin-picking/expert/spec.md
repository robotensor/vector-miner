# vector-miner: bin-picking-expert spec

The miner kit of the leaf `bin-picking-expert`: write an expert, evaluate it locally, pay the fee, commit its
image once per hotkey, serve phase 1, follow the submission and verify it. The miner guide is `README.md` here.

## 1. Files

| path | content |
|---|---|
| `vector_miner/skills/bin_picking/__init__.py` | empty |
| `vector_miner/skills/bin_picking/expert/__init__.py` | `ID = "bin-picking-expert"`, `SKILL = "bin-picking"`, `KIND = "expert"`; imports nothing |
| `vector_miner/skills/bin_picking/expert/cli.py` | `main(argv=None, prog="vector-miner") -> int`: the commands of section 2 |
| `vector_miner/skills/bin_picking/expert/api.py` | `ExpertApi` (section 3) |
| `vector_miner/skills/bin_picking/expert/{board,local,serve,subverify,verify}.py` | status, local eval, phase 1, verify |
| `skills/bin-picking/expert/example/` | the example expert: `solve.mjs`, `package.json`, `package-lock.json`, `Dockerfile`, `README.md` |
| `skills/bin-picking/expert/base/` | the base image: `Dockerfile`, `build.sh`, `expert.mjs`, `README.md` |
| `skills/bin-picking/expert/tests/` | tests, their `conftest.py`, `fake_docker.py`, `fake_expert.py`, `fake_play.py` |
| `skills/bin-picking/expert/README.md` | the miner guide |

Modules moved together from `vector_miner/`, so relative imports between them are unchanged. They import shared
modules absolutely (`from vector_miner import protocol`, `from vector_miner.api import Api, ApiError`).

## 2. Commands

`commit`, `status`, `fee status`, `fee pay`, `serve`, `eval` and `verify` keep every flag, default, environment
variable (`VECTOR_MINER_API`), output line, JSON field and exit code, with these additions:

- `status` output gains `leaf`: `{"id", "status", "round", "next"}` copied from the leaf view's `leaf` object
  when present, else `null`. When `leaf.status` is not `ongoing`, `status` also prints `vector-miner:
  bin-picking-expert is <status>: it plays no phase 1 now` to stderr, followed by `; its next round starts at
  <next.starts_at>` when `leaf.next` is set.
- `commit` (with the API check) reads `leaf.status` of the leaf view, before the submission and fee checks:
  - `finished` or `archived`: refuses before anything is sent: `vector-miner: bin-picking-expert is <status>: it
    takes no commits (--no-api-check skips this check)`.
  - `planned` or `paused`: prints `vector-miner: warning: bin-picking-expert is <status>: the commit is picked up
    when its next round starts` to stderr and goes on.
  - `ongoing`, no `leaf` object, or a leaf view that cannot be read: goes on as before (the later checks report
    an API error as before).
- `serve` handles the refusal `not_running` (`{"type": "error", "reason": "not_running", "message", "retry_at"?}`,
  then close `4403` with reason `not_running: <message>`) like `not_open`: with a `retry_at` it waits until then and
  connects again; without one it exits 1 with the reason.
- The ranked verdict reads `rank <k> · <share> of bin-picking-expert's emission` (`(the king)` appended for rank 1).
- Scores are signed numbers with 3 decimals (`+0.712`, `-1.250`, `0.000`), never percentages. `status` copies
  the board's `phase2_eval` and each row's `eval_score`, `final_score`, `phase2_eval`, `phase1.outs`,
  `phase1.mean_time_s` when the API gives them; `passed` and `not_ranked` verdicts use the final score.
- `subverify`: with `score_formula` (formula `pile-1` or `pile-2`) the phase-1 score is the mean of
  `results.score` over the counted scenes, each recomputed from the formula, `task.time_limit_s` and the scene's
  `picks` (divided by `picks_planned`, `task.picks` or the picks listed) or its own success/t/outs/tcp_path_m;
  `final_score` = `phase2_eval.score`, the private score (w = `phase2_eval.weight`, 1 when absent: (1 − w)·phase 1 +
  w·score; phase 1 without a private evaluation); the round decision
  uses the final score, the gate the phase-1 score; both must be above `min_score` (0 when absent). Documents
  without `score_formula` keep the success-rate check.
- `subverify` audit: a document whose phase 2 has `audited` is checked as an audit. `phase1.score` is the claimed mean
  and `phase1.audited_score` the mean with the audited scenes' scores in place of the claimed ones. Each audited scene
  (`phase2.scenes`) is recomputed by `audit_scene` from the published claimed picks (`phase1.results.picks`) and the
  re-run's picks (`phase2.picks`): picks are compared in order (`same_pick`: action hash, target, outcome, tick, outs,
  parts out); the first claimed pick that differs scores `phase2.fake_pick_score`, every other pick the re-run's gain
  (`score`); a claim whose outcome is a cut-off (`wall_timeout`, `step_timeout`, `disconnected`) is compared up to its
  open pick; the outs penalty is the re-run's. The score and the faked pick must equal `phase2.audited[index]`
  (`audit_problems`, a document problem otherwise). `phase2_check.mismatched` lists the scenes with a faked pick; a
  phase 2 passes with a faked pick, and fails on a `wall_timeout` or a pick over `wall_s`. A document without `audited`
  keeps the exact-match rule on the action hashes. A scene row with `bury: false` is regenerated from its seed without
  the bury (`verify._without_bury`).
- The leaf commands take the wallet flags from `vector_miner.wallet.common()`. The module keeps a module-level
  `_keypair` that the commands call (tests replace it).

- `serve` (phase 1 on the miner's machine, section 5) replaces the step bridge: `--concurrency N` is an optional cap
  on the scenes run at once here (default: none, the server's `scenes_at_once`); `--warm` is accepted and has no effect
  (one note line on stderr); `--stderr DIR` keeps each scene's expert stderr in `DIR/<index:04d>-<scene_id>.log`.
  `serve` needs a vector-env with `harness.check_claim` (else it exits with one line).
- `serve` stops with exit 1 and one line (`serve.mismatch(welcome)`) when it cannot reproduce what the subnet re-runs:
  the welcome's `phase1` is not `scene` (the old relay mode: run the vector-miner of the round you committed in), or
  the welcome's `env` `{version, sim}` differs from this machine's vector-env version or vector-sim code pin
  (`serve.local_pin()`; set up the round tag with `scripts/setup.sh`). A welcome without `phase1` or `env` is played.
- `eval` runs its scenes with `harness.run` (the phase-1 path) and reports `expert_time` (`wall_s`, `pick_max_s`,
  `pick_mean_s`, `picks`, `over_scenes`) instead of `deadlines`; `--strict` exits 1 when a pick's `expert_s` is over
  `--wall`. `--first-step-deadline` and `--step-deadline` are accepted, hidden, and have no effect (one note line).
- `status`: the `phase1` verdict is `phase 1: <done>/<total> scenes answered by your miner (run on your machine)`
  `; claimed score so far <score> (phase 2 audits a sample)` (when the API gives a score) `; keep serve running`.

## 3. API

`ExpertApi(vector_miner.api.Api)`, unchanged paths and checks:

| method | request |
|---|---|
| `expert()` | `GET /v1/skills/bin-picking/expert` |
| `submissions(hotkey)` | `GET /v1/skills/bin-picking/expert/submissions?hotkey=<ss58>` (rows of that hotkey) |
| `submission(sid)` | `GET /v1/skills/bin-picking/expert/submissions/<id>` |
| `submission_verify(sid, max_bytes)` | `GET /v1/skills/bin-picking/expert/submissions/<id>/verify` (bytes) |
| `fee(hotkey)` | `GET /v1/skills/bin-picking/expert/fee?hotkey=<ss58>` |
| `nonce()` | `GET /v1/expert/nonce` |
| `dev_commit(image)`, `dev_fee_pay(tao=None)` | signed `POST /v1/dev/commit`, `/v1/dev/fee/pay` (a dev API without a chain) |

Phase 1 connects to `WS /v1/expert/connect` with the signed hello of `vector_miner.protocol`.

The leaf view carries `leaf`: `{"id": "bin-picking-expert", "skill", "kind", "name", "status": "planned" |
"ongoing" | "paused" | "finished" | "archived", "round": {n, starts_at, ends_at} | null, "next": {n, starts_at,
ends_at} | null, "final_round", "round_mode", "interval_s", "fee"}`. The kit reads only `id`, `status`, `round` and
`next`, and works the same when `leaf` is missing. A competition that is not shown on the site answers `404`
(`no such competition`) on its leaf paths; the commands report it as any other API error.

## 4. Changes inside the leaf

- `local`, `verify`, `subverify` and `serve` import vector-env only by the names listed in the repository spec,
  inside functions, as before. The step bridge (`Launcher`, `Expert`, `Warm`, `Bridge`) and `local.Local` /
  `local.latency` are gone.

## 5. Phase 1: `serve`

The server sends whole scenes; the miner's machine runs each with vector-env's harness and answers with its claim.

```
server -> {"type":"welcome", submission, status, done, scenes, scenes_at_once, scene_deadline_s, phase1, env:{version, sim}}
server -> {"type":"scene","index":i,"scene":{scene_id, seed, xml, meta, checkpoint, state_hash, target, target_class,
          env},"limits":{time_limit_s, picks, max_rows, tick_s, control_hz, wall_s, score, max_abs?}}
miner  -> {"type":"result","index":i,"result":harness.wire_result(result, limits)}   # no action rows: the hashes only
server -> {"type":"ack","index":i,"ok":bool,"reason":str|null,"score":float|null}
server -> {"type":"done", ...} | {"type":"error", reason, message, index?}
```

- `serve.Runner(expert, docker=None, stderr_dir=None, cap=None, log=_log, argv=None)`: `on_scene(m)` starts one
  process per scene (`argv`, default `[python, "-c", CHILD]`, own session) that runs `serve.play(job)`:
  `harness.run(expert, [scene], time_limit_s, wall_s=limits.wall_s or env.yaml, docker, stderr_dir,
  bundle=assets.for_scene(scene))`, then `wire_result(r, limits)` and `check_claim(scene, claim, limits)`; a failure
  there answers `harness_error` with no action hash (`serve.failed`). A claim that fails `check_claim` is still sent, with
  a warning. Image experts get `--label vector-miner.serve=<tag>` (docker `args`), so `close()` kills exactly this
  miner's containers (`docker ps --filter label=...`, `docker kill`), then each scene's process group.
- Answers outlive a connection: `claims[index]` is kept, `unacked` holds the indexes without an `ack`; every
  `welcome` re-sends the unacked ones. A `scene` whose index and `scene_id` are running or answered is not run
  again (an answered one is sent again); the same index with another `scene_id` runs that scene.
- `serve.serve(api, image, keypair, runner, *, stop=None, backoff=BACKOFF, log=_log, max_attempts=None)` keeps the
  hello, the refusal handling, the backoff, `done` and `review` exactly as before; a dropped connection leaves the
  runs going. `serve.run(api, image, keypair, runner)` adds the signal handlers.
- Log lines: one per scene answered (`scene <i> (<id>): <outcome> (<k>/<P> picks, <outs> out, t=..., expert ...),
  claimed score <s>  [<answered>/<total> scenes answered, claimed score <mean> over <n> here (<a> accepted, <r>
  rejected)]`), a rejected ack, the welcome (`the server sends N scenes at a time; each scene must be answered
  within ...`), and at the end `done: <n> scenes answered here (<outcome counts>; claimed score <mean>)`.

## 6. Example and base image

- `example/package.json`: `"@vector/sim": "file:../../../../../vector-sim/js"` (vector-sim beside this checkout);
  `package-lock.json` regenerated; `solve.mjs` unchanged.
- `example/Dockerfile`: `ARG VECTOR_SIM_GIT=https://github.com/robotensor/vector-sim`, `ARG
  VECTOR_SIM_REF=bin-picking-expert-r2` (the round tag: `scripts/setup.sh` reads this line); fetches vector-sim at
  the ref into `/vector-sim` and runs `npm ci --omit=dev` in its `js/`; `WORKDIR /x/skills/bin-picking/expert/example`;
  copies `package.json`, `package-lock.json` and `*.mjs`; `npm ci --omit=dev`; `ENV VECTOR_SIM_BUNDLE=/bundle
  HOME=/tmp`; `USER 65534:65534`; `ENTRYPOINT ["node", "solve.mjs"]`.
- `base/build.sh [tag]`: vector-sim from `VECTOR_SIM_DIR` (default `vector-sim` beside this checkout) at
  `VECTOR_SIM_REF` (default the `ARG VECTOR_SIM_REF=` of `../example/Dockerfile`), exported with `git archive`;
  default image tag `vector-expert-base:<ref>`; labels `vector-sim.ref` and `vector-sim.commit`.
- `base/Dockerfile` and `base/expert.mjs` unchanged.

## 7. Tests

- `skills/bin-picking/expert/tests/conftest.py`: an autouse fixture setting `VECTOR_ENV_SCENE_CACHE` to a temporary
  folder, and the helpers the test modules share.
- Test modules: `test_commit.py` (commit, fee), `test_status.py` (status, verdicts), `test_eval.py` (eval,
  determinism, expert time), `test_serve.py` (a fake subnet; `fake_play.py` as the scene process; with a bundle, real
  scenes through the harness as a command and a fake docker, every answer checked with `check_claim`),
  `test_subverify.py`, `test_verify.py`. The example is
  `HERE.parent / "example" / "solve.mjs"`.
- Shared tests (`tests/test_protocol.py`, `tests/test_chain.py`, `tests/test_dispatch.py`, `tests/test_setup.py`)
  are the repository's.
