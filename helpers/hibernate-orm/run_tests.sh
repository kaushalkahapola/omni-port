#!/bin/bash
# Runs targeted Hibernate ORM tests inside Docker.
# Compatible with evaluate_full_workflow.py — uses the same IMAGE_TAG and volumes
# as run_build.sh so compiled Gradle outputs are reused.
set -e

echo "=== Running Hibernate ORM Tests for ${COMMIT_SHA:0:7} ==="
echo "Target: ${TEST_TARGETS}"

IMAGE_TAG="${IMAGE_TAG:-retrofit-hibernate-orm-builder:local}"

MAX_CPU="${MAX_CPU:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 1)}"
echo "--- Using Docker Image: ${IMAGE_TAG} ---"
echo "CPU detected: ${MAX_CPU}"

# 1. Determine Gradle test command from TEST_TARGETS.
#
# TEST_TARGETS format:
#   "module:ClassName"   →  e.g. "hibernate-core:org.hibernate.orm.test.bytecode.enhance.version.ReEnhancementTests"
#   Space-separated for multiple targets.
#   "ALL"  — run everything
#   "NONE" — skip
#
if [ "${TEST_TARGETS:-}" == "ALL" ]; then
    GRADLE_ARGS="test --no-daemon"
elif [ -n "${TEST_TARGETS:-}" ] && [ "${TEST_TARGETS}" != "NONE" ]; then
    # Build a list of Gradle task args per target.
    # Format: module:class -> :module:test --tests "class"
    GRADLE_ARGS=""

    for target in ${TEST_TARGETS}; do
        module="${target%%:*}"
        cls="${target#*:}"
        gradle_module=":${module//\//:}"
        gradle_module="${gradle_module//::/:}"

        if [ -n "${GRADLE_ARGS}" ]; then
            GRADLE_ARGS="${GRADLE_ARGS} ${gradle_module}:test"
        else
            GRADLE_ARGS="${gradle_module}:test"
        fi
        GRADLE_ARGS="${GRADLE_ARGS} --tests \"${cls}\""
    done
    GRADLE_ARGS="${GRADLE_ARGS} --no-daemon"
elif [ -n "${TEST_MODULES:-}" ]; then
    # Module-level fallback — run all tests in the module.
    GRADLE_ARGS=""
    IFS=',' read -ra MODS <<< "${TEST_MODULES}"
    for mod in "${MODS[@]}"; do
        gradle_module=":${mod//\//:}"
        gradle_module="${gradle_module//::/:}"
        if [ -n "${GRADLE_ARGS}" ]; then
            GRADLE_ARGS="${GRADLE_ARGS} ${gradle_module}:test"
        else
            GRADLE_ARGS="${gradle_module}:test"
        fi
    done
    GRADLE_ARGS="${GRADLE_ARGS} --no-daemon"
elif [ "${TEST_TARGETS:-}" == "NONE" ]; then
    echo "No relevant test targets. Skipping tests."
    exit 0
else
    echo "No TEST_TARGETS or TEST_MODULES set. Skipping tests."
    exit 0
fi

# Create gradle cache volumes
docker volume create gradle-cache-hibernate 2>/dev/null || true
docker volume create gradle-wrapper-hibernate 2>/dev/null || true

# init.gradle (mounted into the container) disables failOnNoMatchingTests so
# wildcard --tests patterns matching zero classes don't abort the whole task.
# --continue lets Gradle keep going past test failures, ensuring XMLs are
# written for every module that did run before the build is finalized.
HELPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INIT_GRADLE_HOST="${HELPER_DIR}/init.gradle"
GRADLE_CMD="./gradlew ${GRADLE_ARGS} --init-script /tmp/retrofit-init.gradle --continue --max-workers=${MAX_CPU} --project-cache-dir /tmp/gradle-project-cache"
echo "--- Executing: ${GRADLE_CMD} ---"

run_gradle_cmd() {
    local cmd="$1"
    local log_file="$2"
    if [ -z "${log_file}" ]; then
        log_file="/tmp/retrofit-hibernate-tests.log"
    fi

    set +e
    docker run --rm \
        -e HOME=/tmp \
        -e XDG_CONFIG_HOME=/tmp \
        -v "gradle-cache-hibernate:/home/gradle/.gradle/caches" \
        -v "gradle-wrapper-hibernate:/home/gradle/.gradle/wrapper" \
        -v "${PROJECT_DIR}:/repo" \
        -v "${INIT_GRADLE_HOST}:/tmp/retrofit-init.gradle:ro" \
        -w /repo \
        "${IMAGE_TAG}" \
        bash -c "git config --global --add safe.directory /repo || true; \
        export GRADLE_OPTS=\"\${GRADLE_OPTS:-} -XX:ActiveProcessorCount=${MAX_CPU}\"; \
        mkdir -p /tmp/gradle-project-cache && \
        ${cmd}; \
        GRADLE_EXIT=\$?; \
        echo '--- Test results are available in build/test-results/ ---'; \
        exit \$GRADLE_EXIT" 2>&1 | tee "${log_file}"
    local rc=${PIPESTATUS[0]}
    set -e
    return ${rc}
}

PRIMARY_LOG="/tmp/retrofit-hibernate-tests-primary.log"

if run_gradle_cmd "${GRADLE_CMD}" "${PRIMARY_LOG}"; then
    echo "✅ Tests Passed"
    exit 0
else
    echo "❌ Tests Failed"
    exit 1
fi
