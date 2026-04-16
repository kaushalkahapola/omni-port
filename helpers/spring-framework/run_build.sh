#!/bin/bash
# Builds (compiles) the Spring Framework project inside Docker.
set -e

echo "--- Building Spring Framework for ${COMMIT_SHA:0:7} ---"
cd "${PROJECT_DIR}"

git config --global --add safe.directory "${PROJECT_DIR}"

# Only checkout when NOT in worktree mode.
if [ "${WORKTREE_MODE}" != "1" ]; then
    echo "--- Checking out commit... ---"
    git checkout -f ${COMMIT_SHA}
    git clean -fd
else
    echo "--- Worktree mode: skipping git checkout ---"
fi

BUILDER_IMAGE="${BUILDER_IMAGE_TAG:-${IMAGE_TAG:-retrofit-spring-framework-builder:local}}"

DOCKER_CMD="docker"
if ! docker info > /dev/null 2>&1; then
    if sudo docker info > /dev/null 2>&1; then DOCKER_CMD="sudo docker"; fi
fi

${DOCKER_CMD} volume create gradle-cache-spring 2>/dev/null || true
${DOCKER_CMD} volume create gradle-wrapper-spring 2>/dev/null || true

# Fix permissions
${DOCKER_CMD} run --rm -u root \
    -v "gradle-cache-spring:/home/gradle/.gradle/caches" \
    -v "gradle-wrapper-spring:/home/gradle/.gradle/wrapper" \
    -v "${PROJECT_DIR}:/repo" \
    ${BUILDER_IMAGE} \
    bash -c "chown -R 1000:1000 /home/gradle/.gradle/caches /home/gradle/.gradle/wrapper /repo 2>/dev/null || true"

echo "--- Compiling with Gradle (build -x test) ---"
if ${DOCKER_CMD} run --rm \
    --dns=8.8.8.8 \
    -u 1000:1000 \
    -v "gradle-cache-spring:/home/gradle/.gradle/caches" \
    -v "gradle-wrapper-spring:/home/gradle/.gradle/wrapper" \
    -v "${PROJECT_DIR}:/repo" \
    -w /repo \
    ${BUILDER_IMAGE} \
    bash -c "git config --global --add safe.directory /repo; \
             find /home/gradle/.gradle/caches -name '*.lock' -delete 2>/dev/null || true; \
             export GRADLE_OPTS='-Dorg.gradle.internal.publish.checksums.insecure=true -Dorg.gradle.scan.publish=false'; \
             ./gradlew build -x test -x checkstyleNohttp --no-daemon"; then
    echo "--- Build complete for ${COMMIT_SHA:0:7} ---"
    exit 0
else
    echo "--- Build FAILED for ${COMMIT_SHA:0:7} ---"
    exit 1
fi
