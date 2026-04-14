#!/bin/bash
set -e

echo "=== Running Tests for ${COMMIT_SHA:0:7} ==="
echo "Target: ${TEST_TARGETS}"

IMAGE_TAG="${BUILDER_IMAGE_TAG:-${IMAGE_TAG:-retrofit-spring-framework-builder:local}}"
echo "--- Using Docker Image: ${IMAGE_TAG} ---"

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
    if sudo docker info > /dev/null 2>&1; then DOCKER_CMD="sudo docker"; fi
fi

${DOCKER_CMD} volume create gradle-cache-spring 2>/dev/null || true
${DOCKER_CMD} volume create gradle-wrapper-spring 2>/dev/null || true

# Fix permissions on any dirs created as root by previous runs
${DOCKER_CMD} run --rm -u root \
    -v "gradle-cache-spring:/home/gradle/.gradle/caches" \
    -v "gradle-wrapper-spring:/home/gradle/.gradle/wrapper" \
    -v "${PROJECT_DIR}:/repo" \
    "${IMAGE_TAG}" \
    bash -c "chown -R 1000:1000 /home/gradle/.gradle/caches /home/gradle/.gradle/wrapper /repo 2>/dev/null || true"

echo "--- Executing: ${GRADLE_CMD} ---"

if ${DOCKER_CMD} run --rm \
    --dns=8.8.8.8 \
    -u 1000:1000 \
    -v "gradle-cache-spring:/home/gradle/.gradle/caches" \
    -v "gradle-wrapper-spring:/home/gradle/.gradle/wrapper" \
    -v "${PROJECT_DIR}:/repo" \
    -w /repo \
    "${IMAGE_TAG}" \
    bash -c "git config --global --add safe.directory /repo; \
             find /home/gradle/.gradle/caches -name '*.lock' -delete 2>/dev/null || true; \
             export GRADLE_OPTS='-Dorg.gradle.internal.publish.checksums.insecure=true -Dorg.gradle.scan.publish=false'; \
             set +e; \
             ${GRADLE_CMD}; \
             GRADLE_EXIT_CODE=\$?; \
             set -e; \
             mkdir -p /repo/build/all-test-results; \
             find /repo -name 'TEST-*.xml' -exec cp {} /repo/build/all-test-results/ \; 2>/dev/null || true; \
             echo \"Copied \$(ls /repo/build/all-test-results/*.xml 2>/dev/null | wc -l) XML files\"; \
             exit \$GRADLE_EXIT_CODE"; then
    echo "✅ Tests Passed"
    exit 0
else
    echo "❌ Tests Failed"
    exit 1
fi
