#!/bin/bash
# Builds (compiles) the Elasticsearch project inside Docker.
# Compatible with evaluate_full_workflow.py — honours WORKTREE_MODE.
set -e

echo "--- Building Elasticsearch for ${COMMIT_SHA:0:7} ---"

HOST_UID="${HOST_UID:-$(id -u)}"
HOST_GID="${HOST_GID:-$(id -g)}"
echo "--- Container user: ${HOST_UID}:${HOST_GID} ---"

# BUILD_DIR must be outside PROJECT_DIR to prevent recursive Docker build contexts.
BUILD_DIR="${BUILD_DIR:-/tmp/es-build-${COMMIT_SHA:0:7}}"
mkdir -p "${BUILD_DIR}"

MAX_CPU="${MAX_CPU:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 1)}"
echo "CPU detected: ${MAX_CPU}"

echo "--- Changing directory to ${PROJECT_DIR} ---"
cd "${PROJECT_DIR}"

# Only reset repo state when NOT in worktree/patch-applied mode.
# When WORKTREE_MODE=1, the ValidationToolkit has already applied patches
# to the working tree — do NOT wipe them with git checkout/clean.
if [ "${WORKTREE_MODE:-0}" != "1" ]; then
    echo "--- Checking out commit: ${COMMIT_SHA} ---"
    git checkout -f "${COMMIT_SHA}"
    git clean -fd
fi

# Determine Docker command (with or without sudo).
DOCKER_CMD="docker"
if ! docker info > /dev/null 2>&1; then
    if sudo docker info > /dev/null 2>&1; then
        echo "Docker requires sudo. Using 'sudo docker'."
        DOCKER_CMD="sudo docker"
    else
        echo "Warning: Docker not accessible. Proceeding anyway."
    fi
fi

# Create persistent Gradle cache volumes.
${DOCKER_CMD} volume create gradle-cache-es 2>/dev/null || true
${DOCKER_CMD} volume create gradle-wrapper-es 2>/dev/null || true

HELPER_DIR="${TOOLKIT_DIR:-$(dirname "$0")}"
if ${DOCKER_CMD} image inspect "${IMAGE_TAG}" > /dev/null 2>&1; then
    echo "--- Docker image ${IMAGE_TAG} already exists, skipping build ---"
else
    echo "--- Building Docker image: ${IMAGE_TAG} ---"
    ${DOCKER_CMD} build -t "${IMAGE_TAG}" -f "${HELPER_DIR}/Dockerfile" "${HELPER_DIR}"
fi

echo "--- Setting cache permissions ---"
${DOCKER_CMD} run --rm -u root \
    -v "gradle-cache-es:/home/gradle/.gradle/caches" \
    -v "gradle-wrapper-es:/home/gradle/.gradle/wrapper" \
    "${IMAGE_TAG}" \
    chown -R "${HOST_UID}:${HOST_GID}" /home/gradle/.gradle

echo "--- Compiling with Gradle (assemble + testClasses, skip tests) ---"
if ${DOCKER_CMD} run --rm \
    --dns=8.8.8.8 \
    -e HOME=/tmp \
    -e XDG_CONFIG_HOME=/tmp \
    -u "${HOST_UID}:${HOST_GID}" \
    -v "gradle-cache-es:/home/gradle/.gradle/caches" \
    -v "gradle-wrapper-es:/home/gradle/.gradle/wrapper" \
    -v "${PROJECT_DIR}:/repo" \
    -w /repo \
    "${IMAGE_TAG}" \
    bash -c "git config --global --add safe.directory /repo || true; \
    export GRADLE_OPTS=\"\${GRADLE_OPTS:-} -XX:ActiveProcessorCount=${MAX_CPU}\"; \
    ./gradlew classes testClasses \
        --max-workers=${MAX_CPU} \
        -x :benchmarks:classes \
        -x :benchmarks:testClasses \
        --no-daemon \
        -Dbuild.docker=false \
        2>&1"; then
    echo "✅ Build Succeeded"
    exit 0
else
    echo "❌ Build Failed"
    exit 1
fi
