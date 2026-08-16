#!/usr/bin/env bash
set -euo pipefail

# Scan the final image filesystem, including OS packages. pip-audit and npm
# audit cover language dependencies but cannot see vulnerabilities inherited
# from a base image. The scanner image itself is immutable and multi-arch.
TRIVY_IMAGE="aquasec/trivy:0.74.0@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969"

if [[ $# -ne 2 ]]; then
    echo "usage: $0 <runtime-image> <web-image>" >&2
    exit 2
fi

runtime_image=$1
web_image=$2
scan_dir=$(mktemp -d)
trap 'rm -rf "${scan_dir}"' EXIT
mkdir -m 0700 "${scan_dir}/cache" "${scan_dir}/home"

docker save --output "${scan_dir}/runtime.tar" "${runtime_image}"
docker save --output "${scan_dir}/web.tar" "${web_image}"

scan() {
    local archive=$1
    docker run --rm \
        --user "$(id -u):$(id -g)" \
        --env HOME=/scan/home \
        --volume "${scan_dir}:/scan" \
        "${TRIVY_IMAGE}" image \
        --quiet \
        --cache-dir /scan/cache \
        --scanners vuln \
        --severity HIGH,CRITICAL \
        --ignore-unfixed \
        --exit-code 1 \
        --input "/scan/${archive}"
}

scan runtime.tar
scan web.tar
