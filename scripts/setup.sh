#!/usr/bin/env bash
# Set up this checkout for one leaf: vector-env and vector-sim at the round tag, .venv, the example and the assets.
#   scripts/setup.sh     VECTOR_MINER_LEAF=<leaf id> picks the leaf when several are here
# The round tag is ARG VECTOR_SIM_REF= of skills/<skill>/<kind>/example/Dockerfile; VECTOR_ENV_REF and
# VECTOR_SIM_REF override it, VECTOR_ENV_DIR (default ../vector-env), VECTOR_ENV_GIT and VECTOR_SIM_GIT choose the source.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -n "${VECTOR_MINER_LEAF:-}" ]; then
  LEAF_DIR="skills/${VECTOR_MINER_LEAF%-*}/${VECTOR_MINER_LEAF##*-}"
  if [ ! -f "$LEAF_DIR/example/Dockerfile" ]; then
    echo "setup: VECTOR_MINER_LEAF=$VECTOR_MINER_LEAF: no $LEAF_DIR/example/Dockerfile" >&2
    exit 1
  fi
else
  LEAF_DIR= n=0 names=
  for f in skills/*/*/example/Dockerfile; do
    [ -f "$f" ] || continue
    d=${f%/example/Dockerfile}
    LEAF_DIR=$d n=$((n + 1)) names="${names:+$names, }$d"
  done
  if [ "$n" -eq 0 ]; then
    echo "setup: no skills/<skill>/<kind>/example/Dockerfile here" >&2
    exit 1
  elif [ "$n" -gt 1 ]; then
    echo "setup: several leaf folders ($names): set VECTOR_MINER_LEAF=<leaf id>" >&2
    exit 1
  fi
fi
DOCKERFILE="$LEAF_DIR/example/Dockerfile"
ROUND=$(sed -n 's/^ARG VECTOR_SIM_REF=\([^[:space:]]*\).*/\1/p' "$DOCKERFILE")
ENV_DIR=${VECTOR_ENV_DIR:-../vector-env}
ENV_REF=${VECTOR_ENV_REF:-$ROUND}
SIM_REF=${VECTOR_SIM_REF:-$ROUND}
if [ -z "$ENV_REF" ] || [ -z "$SIM_REF" ]; then
  echo "setup: no ARG VECTOR_SIM_REF=<tag> in $DOCKERFILE (or set VECTOR_ENV_REF and VECTOR_SIM_REF)" >&2
  exit 1
fi
check_ref() {
  local dir=$1 ref=$2 name=$3 have want
  have=$(git -C "$dir" rev-parse -q --verify HEAD 2>/dev/null) || return 0
  want=$(git -C "$dir" rev-parse -q --verify "$ref^{commit}" 2>/dev/null) || want=
  if [ -z "$want" ]; then
    echo "setup: warning: $dir does not have $ref; fetch it (git -C $dir fetch --tags) and check it out, or local runs use another $name than the round's" >&2
  elif [ "$have" != "$want" ]; then
    echo "setup: warning: $dir is at ${have:0:12}, not $ref; local runs then use another $name than the round's (git -C $dir checkout $ref)" >&2
  fi
}
if [ ! -d "$ENV_DIR/vector_env" ]; then
  git clone --depth 1 --branch "$ENV_REF" "${VECTOR_ENV_GIT:-https://github.com/robotensor/vector-env}" "$ENV_DIR"
else
  check_ref "$ENV_DIR" "$ENV_REF" vector-env
fi
ENV_SETUP=
if [ ! -d "$ENV_DIR/js/node_modules" ]; then
  ENV_SETUP=1
  VECTOR_ENV_JS_ONLY=1 VECTOR_SIM_REF="$SIM_REF" "$ENV_DIR/scripts/setup.sh"
fi
if command -v uv >/dev/null 2>&1; then
  uv venv --allow-existing .venv --python "${PYTHON:-3.12}"
  uv pip install --python .venv/bin/python --require-hashes -r requirements.lock
  uv pip install --python .venv/bin/python --no-deps -e "$ENV_DIR" -e .
else
  "${PYTHON:-python3}" -m venv .venv
  .venv/bin/pip install --require-hashes -r requirements.lock
  .venv/bin/pip install --no-deps -e "$ENV_DIR" -e .
fi
SIM_DIR="$(cd "$ENV_DIR/.." && pwd)/vector-sim"
if [ ! -e ../vector-sim ] && [ -d "$SIM_DIR/js" ]; then
  ln -s "$SIM_DIR" ../vector-sim
  echo "example: linked ../vector-sim -> $SIM_DIR (the @vector/sim of $LEAF_DIR/example is file:../../../../../vector-sim/js)"
fi
if [ -d ../vector-sim/js ]; then
  [ -n "$ENV_SETUP" ] || check_ref ../vector-sim "$SIM_REF" vector-sim
  (cd "$LEAF_DIR/example" && npm ci --no-audit --no-fund)
else
  echo "example: no vector-sim beside this folder or beside $ENV_DIR; clone it to ../vector-sim at $SIM_REF (git clone --branch $SIM_REF ${VECTOR_SIM_GIT:-https://github.com/robotensor/vector-sim} ../vector-sim), then (cd $LEAF_DIR/example && npm ci)" >&2
fi
if ! err=$(.venv/bin/vector-env assets pull 2>&1 >/dev/null); then
  echo "assets: not installed yet (${err#vector-env: }); set VECTOR_ENV_ASSETS_SOURCE (hf://<repo> or a folder) or VECTOR_ENV_BUNDLE, then .venv/bin/vector-env assets pull" >&2
fi
