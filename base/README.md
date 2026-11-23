# Base image

`vector-expert-base` is a starting image for your expert. It contains Node 24, `@vector/sim` at the version the
harness runs (in `/vector-sim/js`, installed in `/expert`), and a placeholder expert at `/expert/expert.mjs`.

The placeholder only shows the `vector-expert/2` message loop: it holds the start pose with the gripper open, so
every scene ends `timed_out` (score 0). It passes the deadline and determinism checks. `example/` is the same
loop with a fixed blind motion, packaged with `@vector/sim` and a Dockerfile.

## Use it

Replace `expert.mjs` with your own, keeping the same message loop:

```dockerfile
FROM <registry>/vector-expert-base:0.1.0
COPY expert.mjs /expert/
```

`import { Cell, IK } from '@vector/sim'` works from any file in `/expert`. Extra npm packages need `USER root`,
`RUN npm install <pkg>` and then `USER 65534:65534` again.

```bash
.venv/bin/vector-miner eval --command "node base/expert.mjs" --scenes 3 --seed 1     # no docker
.venv/bin/vector-miner eval --image <your image> --scenes 30 --seed 1 --workers 4
.venv/bin/vector-miner eval --image <your image> --determinism 20 --seed 7 --workers 4
```

## Build it (maintainers)

```bash
base/build.sh                          # vector-expert-base:0.1.0 from ../../vector-sim at tag v0.1.0
VECTOR_SIM_REF=v0.2.0 base/build.sh    # after a new vector-sim release
```

`build.sh` exports the vector-sim tag with `git archive`, so uncommitted changes in that checkout never get in. The
image records the tag and commit as the labels `vector-sim.ref` and `vector-sim.commit`.
