# Base image

`vector-expert-base` is a starting image for your expert. It contains Node 24, `@vector/sim` of the round the
harness runs (in `/vector-sim/js`, installed in `/expert`), and a placeholder expert at `/expert/expert.mjs`.

The placeholder only shows the `vector-expert/2` message loop: it holds the start pose with the gripper open, so
every scene ends `timed_out` (score 0). It passes the deadline and determinism checks. [../example/](../example/README.md)
is the same loop with a fixed blind motion, packaged with `@vector/sim` and a Dockerfile.

## Use it

Replace `expert.mjs` with your own, keeping the same message loop. The image is tagged with the round tag:

```dockerfile
FROM <registry>/vector-expert-base:bin-picking-expert-r1
COPY expert.mjs /expert/
```

`import { Cell, IK } from '@vector/sim'` works from any file in `/expert`. Extra npm packages need `USER root`,
`RUN npm install <pkg>` and then `USER 65534:65534` again.

From the vector-miner folder:

```bash
.venv/bin/vector-miner eval --command "node skills/bin-picking/expert/base/expert.mjs" --scenes 3 --seed 1   # no docker
.venv/bin/vector-miner eval --image <your image> --scenes 30 --seed 1 --workers 4
.venv/bin/vector-miner eval --image <your image> --determinism 20 --seed 7 --workers 4
```

## Build it (maintainers)

```bash
skills/bin-picking/expert/base/build.sh                                  # vector-expert-base:bin-picking-expert-r1
VECTOR_SIM_REF=<round tag> skills/bin-picking/expert/base/build.sh       # vector-expert-base:<round tag>
```

The round tag defaults to `ARG VECTOR_SIM_REF=` of `../example/Dockerfile`; the source is the vector-sim beside
vector-miner (`VECTOR_SIM_DIR` sets another checkout). `build.sh [tag]` sets another image tag. It exports the tag
with `git archive`, so uncommitted changes in that checkout never get in. The image records the tag and commit as the
labels `vector-sim.ref` and `vector-sim.commit`.
