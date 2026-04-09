#!/bin/bash
# Runs targeted Elasticsearch tests inside Docker.
# Compatible with evaluate_full_workflow.py — uses the same IMAGE_TAG and volumes
# as run_build.sh so compiled Gradle outputs are reused.
set -e

echo "=== Running Elasticsearch Tests for ${COMMIT_SHA:0:7} ==="
echo "Target: ${TEST_TARGETS}"

HOST_UID="${HOST_UID:-$(id -u)}"
HOST_GID="${HOST_GID:-$(id -g)}"
echo "--- Container user: ${HOST_UID}:${HOST_GID} ---"

IMAGE_TAG="${IMAGE_TAG:-retrofit-elasticsearch-builder:local}"

MAX_CPU="${MAX_CPU:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 1)}"
echo "--- Using Docker Image: ${IMAGE_TAG} ---"
echo "CPU detected: ${MAX_CPU}"

# 1. Determine Gradle test command from TEST_TARGETS.
#
# TEST_TARGETS format (set by ValidationToolkit.run_relevant_tests):
#   "module:ClassName"   →  e.g. "server:org.elasticsearch.index.FooTests"
#   Space-separated for multiple targets.
#   "ALL"  — run everything
#   "NONE" — skip
#
if [ "${TEST_TARGETS:-}" == "ALL" ]; then
    GRADLE_ARGS="test --no-daemon -Dbuild.docker=false"
elif [ -n "${TEST_TARGETS:-}" ] && [ "${TEST_TARGETS}" != "NONE" ]; then
    # Build a list of Gradle task args per target.
    # Prefer source-set specific tasks when detectable from changed file hints:
    # - src/internalClusterTest -> :module:internalClusterTest --tests Class
    # - src/test                -> :module:test --tests Class
    GRADLE_ARGS=""
    TEST_TARGET_FILES_LIST=()
    if [ -n "${TEST_TARGET_FILES:-}" ]; then
        IFS=',' read -ra TEST_TARGET_FILES_LIST <<< "${TEST_TARGET_FILES}"
    fi

    resolve_test_tasks() {
        local module="$1"
        local cls="$2"
        local class_file
        class_file="${cls//./\/}.java"

        # 1) Prefer concrete paths from TEST_TARGET_FILES (accurate for source set).
        local file
        for file in "${TEST_TARGET_FILES_LIST[@]}"; do
            [ -z "${file}" ] && continue
            if [[ "${file}" == *"${class_file}" ]]; then
                if [[ "${file}" == */src/internalClusterTest/* ]]; then
                    echo "internalClusterTest"
                    return
                fi
                if [[ "${file}" == */src/javaRestTest/* ]]; then
                    echo "javaRestTest"
                    return
                fi
                if [[ "${file}" == */src/yamlRestTest/* ]]; then
                    echo "yamlRestTest"
                    return
                fi
                if [[ "${file}" == */src/test/java/* ]]; then
                    if [[ "${module}" == x-pack/qa/* ]] || [[ "${module}" == qa/* ]]; then
                        # Under ES QA, bare `test` is often ambiguous; javaRestTest is
                        # the usual entry point for src/test/java REST-style ITs.
                        echo "javaRestTest,internalClusterTest"
                        return
                    fi
                    # Standard unit tests live under src/test/java. Do not append
                    # internalClusterTest here: many plugins (e.g. x-pack/plugin/deprecation)
                    # do not register that task, and Gradle fails the whole invocation
                    # if any listed task is missing.
                    echo "test"
                    return
                fi
            fi
        done

        # 2) No file hint: avoid the old x-pack/qa -> internalClusterTest-only rule
        #    (wrong for e.g. rolling-upgrade, which has no internalClusterTest task).
        if [[ "${module}" == x-pack/qa/* ]] || [[ "${module}" == qa/* ]]; then
            echo "javaRestTest,internalClusterTest"
            return
        fi

        echo "internalClusterTest,test"
    }

    append_target_args() {
        local mode="$1"
        local out=""
        for target in ${TEST_TARGETS}; do
            module="${target%%:*}"
            cls="${target#*:}"
            gradle_module=":${module//\//:}"
            gradle_module="${gradle_module//::/:}"

            if [ "${mode}" == "primary" ]; then
                test_tasks_csv="$(resolve_test_tasks "${module}" "${cls}")"
            else
                # Auto-fix retry: never use bare `test` first on ES QA modules — it is
                # frequently ambiguous (Gradle abbrev expansion). Prefer flipping
                # javaRestTest vs internalClusterTest instead.
                if [[ "${module}" == x-pack/qa/* ]] || [[ "${module}" == qa/* ]]; then
                    test_tasks_csv="internalClusterTest,javaRestTest"
                else
                    test_tasks_csv="test,internalClusterTest"
                fi
            fi

            IFS=',' read -ra TEST_TASKS <<< "${test_tasks_csv}"
            for test_task in "${TEST_TASKS[@]}"; do
                [ -z "${test_task}" ] && continue
                if [ -n "${out}" ]; then
                    out="${out} ${gradle_module}:${test_task}"
                else
                    out="${gradle_module}:${test_task}"
                fi
                out="${out} --tests \"${cls}\""
            done
        done
        echo "${out} --no-daemon -Dbuild.docker=false -DfailIfNoTests=false"
    }

    GRADLE_ARGS="$(append_target_args primary)"
elif [ -n "${TEST_MODULES:-}" ]; then
    # Module-level fallback — run all tests in the module.
    GRADLE_ARGS=""
    IFS=',' read -ra MODS <<< "${TEST_MODULES}"
    for mod in "${MODS[@]}"; do
        gradle_module=":${mod//\//:}"
        gradle_module="${gradle_module//::/:}"
        GRADLE_ARGS="${GRADLE_ARGS} ${gradle_module}:test"
    done
    GRADLE_ARGS="${GRADLE_ARGS} --no-daemon -Dbuild.docker=false"
elif [ "${TEST_TARGETS:-}" == "NONE" ]; then
    echo "No relevant test targets. Skipping tests."
    exit 0
else
    echo "No TEST_TARGETS or TEST_MODULES set. Skipping tests."
    exit 0
fi

DOCKER_CMD="docker"
if ! docker info > /dev/null 2>&1; then
    if sudo docker info > /dev/null 2>&1; then
        echo "Docker requires sudo. Using 'sudo docker'."
        DOCKER_CMD="sudo docker"
    else
        echo "Warning: Docker not accessible. Proceeding anyway."
    fi
fi

${DOCKER_CMD} volume create gradle-cache-es 2>/dev/null || true
${DOCKER_CMD} volume create gradle-wrapper-es 2>/dev/null || true

echo "--- Setting cache permissions ---"
${DOCKER_CMD} run --rm -u root \
    -v "gradle-cache-es:/home/gradle/.gradle/caches" \
    -v "gradle-wrapper-es:/home/gradle/.gradle/wrapper" \
    "${IMAGE_TAG}" \
    chown -R "${HOST_UID}:${HOST_GID}" /home/gradle/.gradle

GRADLE_CMD="./gradlew ${GRADLE_ARGS} --max-workers=${MAX_CPU} --project-cache-dir /tmp/gradle-project-cache"
echo "--- Executing: ${GRADLE_CMD} ---"

run_gradle_cmd() {
    local cmd="$1"
    local log_file="$2"
    if [ -z "${log_file}" ]; then
        log_file="/tmp/retrofit-es-tests.log"
    fi

    set +e
    ${DOCKER_CMD} run --rm \
        --dns=8.8.8.8 \
        -e HOME=/tmp \
        -e XDG_CONFIG_HOME=/tmp \
        -u "${HOST_UID}:${HOST_GID}" \
        -v "gradle-cache-es:/home/gradle/.gradle/caches" \
        -v "gradle-wrapper-es:/home/gradle/.gradle/wrapper" \
        -v "${PROJECT_DIR}:/repo" \
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

is_task_resolution_failure() {
    local log_file="$1"
    [ -z "${log_file}" ] && return 1
    [ ! -f "${log_file}" ] && return 1
    if grep -Eiq "Cannot locate tasks that match|Task 'test' is ambiguous|Task '.+' is ambiguous" "${log_file}"; then
        return 0
    fi
    return 1
}

PRIMARY_LOG="/tmp/retrofit-es-tests-primary.log"
RETRY_LOG="/tmp/retrofit-es-tests-retry.log"

if run_gradle_cmd "${GRADLE_CMD}" "${PRIMARY_LOG}"; then
    echo "✅ Tests Passed"
    exit 0
else
    echo "❌ Tests Failed"

    # One-time auto-fix rerun for ambiguous/missing task selection.
    if [ -n "${TEST_TARGETS:-}" ] && [ "${TEST_TARGETS}" != "NONE" ] && is_task_resolution_failure "${PRIMARY_LOG}"; then
        RETRY_GRADLE_ARGS="$(append_target_args retry)"
        RETRY_GRADLE_CMD="./gradlew ${RETRY_GRADLE_ARGS} --max-workers=${MAX_CPU} --project-cache-dir /tmp/gradle-project-cache"
        echo "--- Auto-fix rerun (task remap): ${RETRY_GRADLE_CMD} ---"
        if run_gradle_cmd "${RETRY_GRADLE_CMD}" "${RETRY_LOG}"; then
            echo "✅ Tests Passed after task auto-fix rerun"
            exit 0
        fi
        if is_task_resolution_failure "${RETRY_LOG}"; then
            echo "RETROFITTEST_RUNNER_CONFIG_UNRESOLVED: Gradle test task resolution failed after one rerun"
            exit 1
        fi
    fi
    exit 1
fi
