#!/usr/bin/env bash
# Build the miner base image from a vector-sim checkout, exported at one tag (the simulator the harness runs).
#   base/build.sh [tag]        default tag: vector-expert-base:<VECTOR_SIM_REF without the v>
# VECTOR_SIM_DIR (default ../../vector-sim) and VECTOR_SIM_REF (default v0.1.0) choose the source.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
sim="${VECTOR_SIM_DIR:-$here/../../vector-sim}"
ref="${VECTOR_SIM_REF:-v0.1.0}"
tag="${1:-vector-expert-base:${ref#v}}"
ctx="$(mktemp -d)"
trap 'rm -rf "$ctx"' EXIT
git -C "$sim" archive "$ref" js | tar -x -C "$ctx"
docker build --build-context vector-sim="$ctx" --label "vector-sim.ref=$ref" \
  --label "vector-sim.commit=$(git -C "$sim" rev-parse "$ref^{commit}")" -t "$tag" "$here"
