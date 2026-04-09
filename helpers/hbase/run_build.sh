#!/bin/bash
# Builds (compiles) the HBase project inside Docker.
# Compatible with evaluate_full_workflow.py — honours WORKTREE_MODE.
set -euo pipefail

WORKTREE_MODE="${WORKTREE_MODE:-0}"
SHORT_SHA="${COMMIT_SHA:-worktree}"
if [ "${WORKTREE_MODE}" != "1" ] && [ -n "${COMMIT_SHA:-}" ]; then
    SHORT_SHA="${COMMIT_SHA:0:7}"
fi

echo "--- Building HBase for commit ${SHORT_SHA} ---"

MAX_CPU="${MAX_CPU:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 1)}"
MAVEN_THREADS="${MAVEN_THREADS:-${MAX_CPU}}"

echo "CPU detected: ${MAX_CPU}"
echo "Maven threads: ${MAVEN_THREADS}"

# Initialize build exit code
BUILD_EXIT_CODE=0

echo "--- Changing directory to ${PROJECT_DIR} ---"
cd "${PROJECT_DIR}"

# Only reset repo state when NOT in worktree/patch-applied mode.
# When WORKTREE_MODE=1, the ValidationToolkit has already applied patches
# to the working tree — do NOT wipe them with git checkout/clean.
if [ "${WORKTREE_MODE}" != "1" ]; then
    echo "--- Checking out commit: ${COMMIT_SHA} ---"
    git checkout -f "${COMMIT_SHA}"
else
    echo "--- WORKTREE_MODE=1: preserving current worktree state ---"
fi

# Create persistent Maven cache volume (reuse across builds)
docker volume create maven-cache-hbase 2>/dev/null || true

echo "--- Running Maven build (compile only, no tests) ---"

# Build without tests, skip documentation and code quality checks
BUILD_COMMAND="mvn clean install -DskipTests -T ${MAVEN_THREADS} \
  -Dmaven.javadoc.skip=true \
  -Dcheckstyle.skip=true \
  -Dfindbugs.skip=true \
  -Dspotbugs.skip=true \
  -Denforcer.skip=true"

docker run --rm \
    --dns=8.8.8.8 \
    -v "${PROJECT_DIR}:/repo" \
    -v "maven-cache-hbase:/root/.m2" \
    -w /repo \
    ${BUILDER_IMAGE_TAG} \
    bash -c "export MAVEN_OPTS=\"\${MAVEN_OPTS:-} -XX:ActiveProcessorCount=${MAX_CPU}\" && rm -rf /root/.m2/repository/org/apache/hbase && ${BUILD_COMMAND}" \
    || BUILD_EXIT_CODE=$?

# Save build status (only if BUILD_STATUS_FILE is set)
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

echo "--- Build complete for ${SHORT_SHA} ---"
exit ${BUILD_EXIT_CODE}
