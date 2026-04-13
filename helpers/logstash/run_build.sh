#!/bin/bash
# This script compiles the Logstash code inside Docker.
set -e

# Determine if we need sudo for docker
DOCKER_CMD="docker"
if ! docker info > /dev/null 2>&1; then
    if sudo docker info > /dev/null 2>&1; then
        echo "Docker requires sudo. Using 'sudo docker'."
        DOCKER_CMD="sudo docker"
    else
        echo "Warning: Docker command failed. Continuing with 'docker'."
    fi
fi

# Use the image tag provided by the harness; fall back to the legacy name.
BUILDER_IMAGE="${BUILDER_IMAGE_TAG:-${IMAGE_TAG:-retrofit-logstash-builder:local}}"

echo "--- Fixing permissions for ${PROJECT_DIR} ---"
${DOCKER_CMD} run --rm -v "${PROJECT_DIR}:/repo" alpine chown -R $(id -u):$(id -g) /repo

echo "--- Building code for ${COMMIT_SHA:0:7} ---"
cd "${PROJECT_DIR}"
git config --global --add safe.directory "${PROJECT_DIR}"

# Only checkout the commit when NOT in worktree mode.
# In worktree mode the pipeline has already applied patches to the working tree;
# checking out would discard them.
if [ "${WORKTREE_MODE}" != "1" ]; then
    echo "--- Checking out commit... ---"
    git checkout ${COMMIT_SHA}
else
    echo "--- Worktree mode: skipping git checkout, building current working tree ---"
fi

${DOCKER_CMD} volume create gradle-cache-ls 2>/dev/null || true
${DOCKER_CMD} volume create gradle-wrapper-ls 2>/dev/null || true

echo "--- Setting cache permissions... ---"
${DOCKER_CMD} run --rm -u root \
    -v "gradle-cache-ls:/home/gradle/.gradle/caches" \
    -v "gradle-wrapper-ls:/home/gradle/.gradle/wrapper" \
    -v "${PROJECT_DIR}:/repo" \
    ${BUILDER_IMAGE} \
    chown -R 1000:1000 /home/gradle/.gradle/caches /home/gradle/.gradle/wrapper /repo

echo "--- Compiling and preparing for tests... ---"
if ${DOCKER_CMD} run --rm \
    --dns=8.8.8.8 \
    -u 1000:1000 \
    -v "gradle-cache-ls:/home/gradle/.gradle/caches" \
    -v "gradle-wrapper-ls:/home/gradle/.gradle/wrapper" \
    -v "${PROJECT_DIR}:/repo" \
    ${BUILDER_IMAGE} \
    bash -c "find /home/gradle/.gradle/caches -name '*.lock' -delete; ./gradlew classes testClasses -Dbuild.docker=false --continue"; then
    echo "--- Build complete for ${COMMIT_SHA:0:7} ---"
    exit 0
else
    echo "--- Build FAILED for ${COMMIT_SHA:0:7} ---"
    exit 1
fi
