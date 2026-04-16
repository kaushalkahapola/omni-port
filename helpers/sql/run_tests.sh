#!/bin/bash
set -e

echo "=== Running Tests for ${COMMIT_SHA:0:7} ==="
echo "Target: ${TEST_TARGETS}"

# Use the image tag provided by the harness.
IMAGE_TAG="${BUILDER_IMAGE_TAG:-${IMAGE_TAG:-retrofit-sql-builder:local}}"

echo "--- Using Docker Image: ${IMAGE_TAG} ---"

# Configure Gradle test command from TEST_TARGETS.
# Targets from get_test_targets.py look like:
#   ":module:test --tests org.opensearch.sql.FooTests"
# Multiple targets are space-separated.
if [ "${TEST_TARGETS}" == "ALL" ]; then
    GRADLE_CMD="./gradlew test"
elif [ "${TEST_TARGETS}" == "NONE" ] || [ -z "${TEST_TARGETS}" ]; then
    echo "No relevant source code changes found. Skipping tests."
    exit 0
else
    GRADLE_CMD="./gradlew ${TEST_TARGETS}"
fi

DOCKER_CMD="docker"
if ! docker info > /dev/null 2>&1; then
    if sudo docker info > /dev/null 2>&1; then
        DOCKER_CMD="sudo docker"
    fi
fi

${DOCKER_CMD} volume create gradle-cache-sql 2>/dev/null || true
${DOCKER_CMD} volume create gradle-wrapper-sql 2>/dev/null || true

# Fix permissions on .gradle dir inside the repo (may have been created as root
# by a previous Docker run, causing checksums.lock permission denied errors).
${DOCKER_CMD} run --rm -u root \
    -v "gradle-cache-sql:/home/gradle/.gradle/caches" \
    -v "gradle-wrapper-sql:/home/gradle/.gradle/wrapper" \
    -v "${PROJECT_DIR}:/repo" \
    "${IMAGE_TAG}" \
    bash -c "chown -R 1000:1000 /home/gradle/.gradle/caches /home/gradle/.gradle/wrapper /repo 2>/dev/null || true"

echo "--- Executing: ${GRADLE_CMD} ---"

if ${DOCKER_CMD} run --rm \
    --dns=8.8.8.8 \
    -u 1000:1000 \
    -v "gradle-cache-sql:/home/gradle/.gradle/caches" \
    -v "gradle-wrapper-sql:/home/gradle/.gradle/wrapper" \
    -v "${PROJECT_DIR}:/repo" \
    -w /repo \
    "${IMAGE_TAG}" \
    bash -c "find /home/gradle/.gradle/caches -name '*.lock' -delete 2>/dev/null || true; \
             set +e; \
             ${GRADLE_CMD}; \
             GRADLE_EXIT_CODE=\$?; \
             set -e; \
             mkdir -p /repo/build/all-test-results; \
             find . -name 'TEST-*.xml' -exec cp {} /repo/build/all-test-results/ \; 2>/dev/null || true; \
             exit \$GRADLE_EXIT_CODE"; then
    echo "✅ Tests Passed"
    exit 0
else
    echo "❌ Tests Failed"
    exit 1
fi
