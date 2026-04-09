#!/bin/bash
# Optimized Druid build script with permission fix
set -euo pipefail

WORKTREE_MODE="${WORKTREE_MODE:-0}"
SHORT_SHA="${COMMIT_SHA:-worktree}"
if [ "${WORKTREE_MODE}" != "1" ] && [ -n "${COMMIT_SHA:-}" ]; then
    SHORT_SHA="${COMMIT_SHA:0:7}"
fi

echo "=== Building Druid for ${SHORT_SHA} ==="

MAX_CPU="${MAX_CPU:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 1)}"
MAVEN_THREADS="${MAVEN_THREADS:-${MAX_CPU}}"

echo "CPU detected: ${MAX_CPU}"
echo "Maven threads: ${MAVEN_THREADS}"

# Initialize build exit code
BUILD_EXIT_CODE=0

cd "${PROJECT_DIR}"

if [ "${WORKTREE_MODE}" != "1" ]; then
    # FIX: Use Docker as root to forcefully clean and fix permissions
    echo "=== Cleaning Docker-created files and fixing permissions ==="
    docker run --rm \
        -v "${PROJECT_DIR}:/repo" \
        -w /repo \
        --user root \
        ${BUILDER_IMAGE_TAG} \
        bash -c "git config --global --add safe.directory /repo && git clean -fdx && chown -R $(id -u):$(id -g) /repo" || true

    # Now checkout can succeed
    echo "=== Checking out commit ${COMMIT_SHA} ==="
    git checkout -f ${COMMIT_SHA}
else
    echo "=== WORKTREE_MODE=1: preserving current worktree state ==="
fi

# Create Maven cache volume (reuse across builds)
docker volume create maven-repo 2>/dev/null || true

echo "=== Running standard Maven build ==="

# Run Maven build
# Reverted to standard install as requested
docker run --rm \
    -v "${PROJECT_DIR}:/repo" \
    -v "maven-repo:/root/.m2/repository" \
    -w /repo \
    ${BUILDER_IMAGE_TAG} \
    bash -c "export MAVEN_OPTS=\"\${MAVEN_OPTS:-} -XX:ActiveProcessorCount=${MAX_CPU}\" && sed -i '/<artifactId>frontend-maven-plugin<\/artifactId>/,/<\/configuration>/ s/<configuration>/<configuration><skip>true<\/skip>/' web-console/pom.xml && mvn clean install -DskipTests -Dweb.console.skip=true -Dmaven.javadoc.skip=true -Dcheckstyle.skip=true -Dpmd.skip=true -Dforbiddenapis.skip=true -Denforcer.skip=true -Drat.skip=true -T ${MAVEN_THREADS} -pl '!:distribution'" \
    || BUILD_EXIT_CODE=$?

# Save build status
if [ ${BUILD_EXIT_CODE} -eq 0 ]; then
        if [ -n "${BUILD_STATUS_FILE:-}" ]; then
            echo "Success" > "${BUILD_STATUS_FILE}"
        fi
        echo "✅ Build succeeded for ${SHORT_SHA}"
else
        if [ -n "${BUILD_STATUS_FILE:-}" ]; then
            echo "Fail" > "${BUILD_STATUS_FILE}"
        fi
        echo "❌ Build failed for ${SHORT_SHA}"
fi

echo "=== Build finished for ${SHORT_SHA} ==="