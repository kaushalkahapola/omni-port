#!/bin/bash
# Builds (compiles) the grpc-java project inside Docker.
# Compatible with evaluate_full_workflow.py — honours WORKTREE_MODE.
set -e

echo "--- Building grpc-java for ${COMMIT_SHA:0:7} ---"

HOST_UID="${HOST_UID:-$(id -u)}"
HOST_GID="${HOST_GID:-$(id -g)}"
echo "--- Container user: ${HOST_UID}:${HOST_GID} ---"

# BUILD_DIR must be outside PROJECT_DIR to prevent recursive Docker build contexts.
BUILD_DIR="${BUILD_DIR:-/tmp/grpc-java-build-${COMMIT_SHA:0:7}}"
mkdir -p "${BUILD_DIR}"

MAX_CPU="${MAX_CPU:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 1)}"
echo "CPU detected: ${MAX_CPU}"

echo "--- Changing directory to ${PROJECT_DIR} ---"
cd "${PROJECT_DIR}"

# Only reset repo state when NOT in worktree/patch-applied mode.
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
${DOCKER_CMD} volume create gradle-cache-grpc-java 2>/dev/null || true
${DOCKER_CMD} volume create gradle-wrapper-grpc-java 2>/dev/null || true

IMAGE_TAG="${IMAGE_TAG:-retrofit-grpc-java-builder:local}"
echo "--- Building Docker image: ${IMAGE_TAG} ---"
HELPER_DIR="${TOOLKIT_DIR:-$(dirname "$0")}"
${DOCKER_CMD} build -t "${IMAGE_TAG}" -f "${HELPER_DIR}/Dockerfile" "${HELPER_DIR}"

echo "--- Setting cache permissions ---"
${DOCKER_CMD} run --rm -u root \
    -v "gradle-cache-grpc-java:/home/gradle/.gradle/caches" \
    -v "gradle-wrapper-grpc-java:/home/gradle/.gradle/wrapper" \
    "${IMAGE_TAG}" \
    chown -R "${HOST_UID}:${HOST_GID}" /home/gradle/.gradle

echo "--- Compiling with Gradle (clean assemble -x test) ---"
if ${DOCKER_CMD} run --rm \
    --dns=8.8.8.8 \
    -e HOME=/tmp \
    -e XDG_CONFIG_HOME=/tmp \
    -u "${HOST_UID}:${HOST_GID}" \
    -v "gradle-cache-grpc-java:/home/gradle/.gradle/caches" \
    -v "gradle-wrapper-grpc-java:/home/gradle/.gradle/wrapper" \
    -v "${PROJECT_DIR}:/repo" \
    -w /repo \
    "${IMAGE_TAG}" \
    bash -c "git config --global --add safe.directory /repo || true; \
    export GRADLE_OPTS=\"\${GRADLE_OPTS:-} -XX:ActiveProcessorCount=${MAX_CPU} -Xmx2g\"; \
    ./gradlew clean assemble -x test \
        -PskipAndroid=true \
        --max-workers=${MAX_CPU} \
        --no-daemon \
        2>&1"; then
    echo "✅ Build Succeeded"
    exit 0
else
    echo "❌ Build Failed"
    exit 1
fi
