#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
: "${MAGICFLOW_VERSION:?Set MAGICFLOW_VERSION to the intended release or local test version}"
revision=$(git rev-parse HEAD)
if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then revision="$revision-dirty"; fi
exec docker build --platform linux/amd64 \
    --build-arg "VERSION=$MAGICFLOW_VERSION" --build-arg "VCS_REF=$revision" \
    -t "${MAGICFLOW_BUILD_IMAGE:-magicflow:local}" "$@" .
