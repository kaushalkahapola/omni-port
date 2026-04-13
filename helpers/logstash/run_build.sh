#!/bin/bash
# This script builds the Docker image and compiles the code.
set -e # Exit on error

# Determine if we need sudo for docker
DOCKER_CMD="docker"
if ! docker info > /dev/null 2>&1; then
    if sudo docker info > /dev/null 2>&1; then
        echo "Docker requires sudo. Using 'sudo docker'."
        DOCKER_CMD="sudo docker"
    else
        echo "Warning: Docker command failed and sudo check failed. Continuing with 'docker' but expect errors."
    fi
fi

echo "--- Fixing permissions for ${PROJECT_DIR} ---"
# We use a lightweight image to chown the directory back to the current user
# This fixes issues where previous Docker runs (as root) messed up .git permissions
${DOCKER_CMD} run --rm -v "${PROJECT_DIR}:/repo" alpine chown -R $(id -u):$(id -g) /repo

echo "--- Building code for ${COMMIT_SHA:0:7} ---"

echo "--- Changing directory to ${PROJECT_DIR} ---"
cd "${PROJECT_DIR}"

echo "--- Checking out commit... ---"
git config --global --add safe.directory "${PROJECT_DIR}"
git checkout ${COMMIT_SHA}

# Create persistent Gradle cache volumes if they don't exist

${DOCKER_CMD} volume create gradle-cache-ls 2>/dev/null || true
${DOCKER_CMD} volume create gradle-wrapper-ls 2>/dev/null || true

echo "--- Building Docker image (Incremental) ... ---"
# Use a fixed tag for the builder image
BUILDER_IMAGE="logstash-builder:latest"
${DOCKER_CMD} build -t ${BUILDER_IMAGE} -f ${TOOLKIT_DIR}/Dockerfile .

echo "--- Setting cache permissions... ---"
${DOCKER_CMD} run --rm -u root \
    -v "gradle-cache-ls:/home/gradle/.gradle/caches" \
    -v "gradle-wrapper-ls:/home/gradle/.gradle/wrapper" \
    -v "${PROJECT_DIR}:/repo" \
    ${BUILDER_IMAGE} \
    chown -R 1000:1000 /home/gradle/.gradle/caches /home/gradle/.gradle/wrapper /repo

echo "--- Compiling and preparing for tests... ---"
# Mount the source code from host to /repo
if ${DOCKER_CMD} run --rm \
    --dns=8.8.8.8 \
    -u 1000:1000 \
    -v "gradle-cache-ls:/home/gradle/.gradle/caches" \
    -v "gradle-wrapper-ls:/home/gradle/.gradle/wrapper" \
    -v "${PROJECT_DIR}:/repo" \
    ${BUILDER_IMAGE} \
    bash -c "find /home/gradle/.gradle/caches -name '*.lock' -delete; ./gradlew classes testClasses -Dbuild.docker=false --continue"; then
    echo "Success" > $BUILD_STATUS_FILE
else
    echo "Fail" > $BUILD_STATUS_FILE
fi

echo "--- Build complete for ${COMMIT_SHA:0:7} ---"