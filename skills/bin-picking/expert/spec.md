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
| `skills/bin-picking/expert/tests/` | tests, their `conftest.py`, `fake_docker.py`, `fake_expert.py` |
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
- The leaf commands take the wallet flags from `vector_miner.wallet.common()`. The module keeps a module-level
  `_keypair` that the commands call (tests replace it).

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

- The containers started ahead are the class `serve.Warm`; `local` uses it.
- `local`, `verify`, `subverify` and `serve` import vector-env only by the names listed in the repository spec,
  inside functions, as before.

## 5. Example and base image

- `example/package.json`: `"@vector/sim": "file:../../../../../vector-sim/js"` (vector-sim beside this checkout);
  `package-lock.json` regenerated; `solve.mjs` unchanged.
- `example/Dockerfile`: `ARG VECTOR_SIM_GIT=https://github.com/robotensor/vector-sim`, `ARG
  VECTOR_SIM_REF=bin-picking-expert-r1` (the round tag: `scripts/setup.sh` reads this line); fetches vector-sim at
  the ref into `/vector-sim` and runs `npm ci --omit=dev` in its `js/`; `WORKDIR /x/skills/bin-picking/expert/example`;
  copies `package.json`, `package-lock.json` and `*.mjs`; `npm ci --omit=dev`; `ENV VECTOR_SIM_BUNDLE=/bundle
  HOME=/tmp`; `USER 65534:65534`; `ENTRYPOINT ["node", "solve.mjs"]`.
- `base/build.sh [tag]`: vector-sim from `VECTOR_SIM_DIR` (default `vector-sim` beside this checkout) at
  `VECTOR_SIM_REF` (default the `ARG VECTOR_SIM_REF=` of `../example/Dockerfile`), exported with `git archive`;
  default image tag `vector-expert-base:<ref>`; labels `vector-sim.ref` and `vector-sim.commit`.
- `base/Dockerfile` and `base/expert.mjs` unchanged.

## 6. Tests

- `skills/bin-picking/expert/tests/conftest.py`: an autouse fixture setting `VECTOR_ENV_SCENE_CACHE` to a temporary
  folder, and the helpers the test modules share.
- Test modules: `test_commit.py` (commit, fee), `test_status.py` (status, verdicts), `test_eval.py` (eval,
  determinism, deadlines), `test_serve.py`, `test_subverify.py`, `test_verify.py`. The example is
  `HERE.parent / "example" / "solve.mjs"`.
- Shared tests (`tests/test_protocol.py`, `tests/test_chain.py`, `tests/test_dispatch.py`, `tests/test_setup.py`)
  are the repository's.
