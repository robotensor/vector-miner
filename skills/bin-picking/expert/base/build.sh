#!/usr/bin/env bash
# Build the miner base image from a vector-sim checkout, exported at one ref (the simulator the harness runs).
#   skills/bin-picking/expert/base/build.sh [tag]   default tag: vector-expert-base:<VECTOR_SIM_REF>
# VECTOR_SIM_DIR (default: vector-sim beside this checkout) and VECTOR_SIM_REF (default: the round tag in
# ARG VECTOR_SIM_REF= of ../example/Dockerfile) choose the source.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
sim="${VECTOR_SIM_DIR:-$here/../../../../../vector-sim}"
ref="${VECTOR_SIM_REF:-$(sed -n 's/^ARG VECTOR_SIM_REF=\([^[:space:]]*\).*/\1/p' "$here/../example/Dockerfile")}"
if [ -z "$ref" ]; then
  echo "build: $here/../example/Dockerfile has no ARG VECTOR_SIM_REF=<tag> and VECTOR_SIM_REF is not set" >&2
  exit 1
fi
tag="${1:-vector-expert-base:$ref}"
ctx="$(mktemp -d)"
trap 'rm -rf "$ctx"' EXIT
git -C "$sim" archive "$ref" js | tar -x -C "$ctx"
docker build --build-context vector-sim="$ctx" --label "vector-sim.ref=$ref" \
  --label "vector-sim.commit=$(git -C "$sim" rev-parse "$ref^{commit}")" -t "$tag" "$here"
