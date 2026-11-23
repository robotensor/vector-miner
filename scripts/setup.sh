#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
ENV_DIR=${VECTOR_ENV_DIR:-../vector-env}
if [ ! -d "$ENV_DIR/vector_env" ]; then
  git clone --depth 1 --branch "${VECTOR_ENV_REF:-main}" "${VECTOR_ENV_GIT:-https://github.com/robotensor/vector-env}" "$ENV_DIR"
fi
if [ ! -d "$ENV_DIR/js/node_modules" ]; then
  VECTOR_ENV_JS_ONLY=1 "$ENV_DIR/scripts/setup.sh"
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
  echo "example: linked ../vector-sim -> $SIM_DIR (the example's @vector/sim is file:../../vector-sim/js)"
fi
if [ -d ../vector-sim/js ]; then
  (cd example && npm ci --no-audit --no-fund)
else
  echo "example: no vector-sim beside this folder or beside $ENV_DIR; clone it to ../vector-sim, then (cd example && npm ci)" >&2
fi
if ! err=$(.venv/bin/vector-env assets pull 2>&1 >/dev/null); then
  echo "assets: not installed yet (${err#vector-env: }); set VECTOR_ENV_ASSETS_SOURCE (hf://<repo> or a folder) or VECTOR_ENV_BUNDLE, then .venv/bin/vector-env assets pull" >&2
fi
