#!/bin/bash
set -e

echo "=== Running Tests for ${COMMIT_SHA:0:7} ==="
echo "Target: ${TEST_TARGETS}"

# Use the image tag passed by the harness, fall back to the legacy name
IMAGE_TAG="${BUILDER_IMAGE_TAG:-${IMAGE_TAG:-logstash-builder:latest}}"

echo "--- Using Docker Image: ${IMAGE_TAG} ---"

# BUILD_DIR: where test results are copied after the run.
# Default to a temp dir so the script works even when not set by the harness.
BUILD_DIR="${BUILD_DIR:-${PROJECT_DIR}/build}"

# 2. Configure Test Command
if [ "${TEST_TARGETS}" == "ALL" ]; then
    # Per documentation, javaTests runs the subset of tests covering the Java codebase only
    GRADLE_CMD="./gradlew javaTests"
elif [ "${TEST_TARGETS}" == "NONE" ]; then
    echo "No relevant source code changes found. Skipping tests."
    exit 0
else
    GRADLE_CMD="./gradlew ${TEST_TARGETS}"
fi

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

# 3. Run Tests in Docker
# Create persistent Gradle cache volumes if they don't exist
${DOCKER_CMD} volume create gradle-cache-ls 2>/dev/null || true
${DOCKER_CMD} volume create gradle-wrapper-ls 2>/dev/null || true

echo "--- Executing: ${GRADLE_CMD} ---"

# Mount source code from host to /repo
if ${DOCKER_CMD} run --rm \
    --dns=8.8.8.8 \
    -u 1000:1000 \
    -v "gradle-cache-ls:/home/gradle/.gradle/caches" \
    -v "gradle-wrapper-ls:/home/gradle/.gradle/wrapper" \
    -v "${PROJECT_DIR}:/repo" \
    "${IMAGE_TAG}" \
    bash -c "find /home/gradle/.gradle/caches -name '*.lock' -delete; ${GRADLE_CMD}; \
    GRADLE_EXIT_CODE=\$?; \
    mkdir -p /repo/build/all-test-results; \
    find . -name 'TEST-*.xml' -exec cp {} /repo/build/all-test-results/ \;; \
    exit \$GRADLE_EXIT_CODE"; then
    
    echo "✅ Tests Passed"
    mkdir -p "${BUILD_DIR}/all-test-results"
    cp -r "${PROJECT_DIR}/build/all-test-results/"* "${BUILD_DIR}/all-test-results/" 2>/dev/null || true
    exit 0
else
    echo "❌ Tests Failed"
    mkdir -p "${BUILD_DIR}/all-test-results"
    cp -r "${PROJECT_DIR}/build/all-test-results/"* "${BUILD_DIR}/all-test-results/" 2>/dev/null || true
    exit 1
fi