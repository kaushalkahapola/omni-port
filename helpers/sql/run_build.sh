#!/bin/bash
# Builds (compiles) the OpenSearch SQL project inside Docker.
set -e

echo "--- Building code for ${COMMIT_SHA:0:7} ---"
cd "${PROJECT_DIR}"

git config --global --add safe.directory "${PROJECT_DIR}"

# Only checkout when NOT in worktree mode — in worktree mode the pipeline has
# already applied patches to the working tree; checking out would discard them.
if [ "${WORKTREE_MODE}" != "1" ]; then
    echo "--- Checking out commit... ---"
    git checkout ${COMMIT_SHA}
else
    echo "--- Worktree mode: skipping git checkout, building current working tree ---"
fi

# Use the image tag provided by the harness.
BUILDER_IMAGE="${BUILDER_IMAGE_TAG:-${IMAGE_TAG:-retrofit-sql-builder:local}}"

# Determine if we need sudo for docker
DOCKER_CMD="docker"
if ! docker info > /dev/null 2>&1; then
    if sudo docker info > /dev/null 2>&1; then
        DOCKER_CMD="sudo docker"
    fi
fi

${DOCKER_CMD} volume create gradle-cache-sql 2>/dev/null || true
${DOCKER_CMD} volume create gradle-wrapper-sql 2>/dev/null || true

echo "--- Setting cache permissions... ---"
${DOCKER_CMD} run --rm -u root \
    -v "gradle-cache-sql:/home/gradle/.gradle/caches" \
    -v "gradle-wrapper-sql:/home/gradle/.gradle/wrapper" \
    -v "${PROJECT_DIR}:/repo" \
    ${BUILDER_IMAGE} \
    bash -c "chown -R 1000:1000 /home/gradle/.gradle/caches /home/gradle/.gradle/wrapper /repo 2>/dev/null || true"

echo "--- Compiling and preparing for tests... ---"
if ${DOCKER_CMD} run --rm \
    --dns=8.8.8.8 \
    -u 1000:1000 \
    -v "gradle-cache-sql:/home/gradle/.gradle/caches" \
    -v "gradle-wrapper-sql:/home/gradle/.gradle/wrapper" \
    -v "${PROJECT_DIR}:/repo" \
    -w /repo \
    ${BUILDER_IMAGE} \
    bash -c "find /home/gradle/.gradle/caches -name '*.lock' -delete 2>/dev/null || true; \
             ./gradlew classes testClasses -Dbuild.docker=false --continue"; then
    echo "--- Build complete for ${COMMIT_SHA:0:7} ---"
    exit 0
else
    echo "--- Build FAILED for ${COMMIT_SHA:0:7} ---"
    exit 1
fi
