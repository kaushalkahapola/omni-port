#!/bin/bash
set -e

WORKTREE_MODE="${WORKTREE_MODE:-0}"
echo "=== Running Tests for ${COMMIT_SHA:0:7} === (WORKTREE_MODE=${WORKTREE_MODE})"
echo "Targets: ${TEST_TARGETS}"

# 1. Configure Test Command
if [ "${TEST_TARGETS}" == "ALL" ]; then
    MAVEN_ARGS=""
elif [ "${TEST_TARGETS}" == "NONE" ]; then
    echo "No relevant source code changes found. Skipping tests."
    exit 0
else
    # TEST_TARGETS is expected to be a space-separated string of "module:class"
    MODULES=""
    TESTS=""
    # Use array to handle spaces if any, though targets usually don't have them
    for target in ${TEST_TARGETS}; do
        if [[ "${target}" == *":"* ]]; then
            mod="${target%%:*}"
            cls="${target#*:}"
            if [ -z "$MODULES" ]; then 
                MODULES="$mod"
            else 
                if [[ ",$MODULES," != *",$mod,"* ]]; then 
                    MODULES="$MODULES,$mod"
                fi
            fi
            if [ -z "$TESTS" ]; then 
                TESTS="$cls"
            else 
                TESTS="$TESTS,$cls"
            fi
        else
            # Fallback for single classes or already formatted args
            if [ -z "$TESTS" ]; then TESTS="$target"; else TESTS="$TESTS,$target"; fi
        fi
    done
    if [ -n "$MODULES" ]; then
        MAVEN_ARGS="-pl ${MODULES} -am -Dtest=${TESTS}"
    else
        MAVEN_ARGS="-Dtest=${TESTS}"
    fi
fi

echo "--- Starting Test Execution ---"
echo "--- Command: mvn test ${MAVEN_ARGS} ---"

# 2. Run Tests
docker volume create maven-repo 2>/dev/null || true
DOCKER_GID=$(stat -c '%g' /var/run/docker.sock 2>/dev/null || echo 0)

CHECKOUT_CMD="git checkout -f ${COMMIT_SHA}"
if [ "${WORKTREE_MODE}" == "1" ]; then
    CHECKOUT_CMD="echo 'Skipping checkout (WORKTREE_MODE=1)'"
fi

if docker run --rm \
    --privileged \
    -v "${PROJECT_DIR}:/repo" \
    -v "maven-repo:/root/.m2/repository" \
    -v "/var/run/docker.sock:/var/run/docker.sock" \
    -e TESTCONTAINERS_RYUK_DISABLED=true \
    -e TESTCONTAINERS_CHECKS_DISABLE=true \
    -e DOCKER_HOST=unix:///var/run/docker.sock \
    --group-add "${DOCKER_GID}" \
    -w /repo \
    "${BUILDER_IMAGE_TAG}" \
    bash -c "chmod 666 /var/run/docker.sock 2>/dev/null || true && docker info && ${CHECKOUT_CMD} && \
             mvn test ${MAVEN_ARGS} -DfailIfNoTests=false -Denforcer.skip=true -Dskip.yarn -Dskip.npm -Dskip.installnodenpm -Dmaven.antrun.skip=true -Dmaven.javadoc.skip=true -Dcheckstyle.skip=true; \
             MVN_EXIT_CODE=\$?; \
             mkdir -p /repo/build/all-test-results; \
             find . -type f -name 'TEST-*.xml' -not -path '*/build/*' -exec cp {} /repo/build/all-test-results/ \;; \
             exit \$MVN_EXIT_CODE"; then
    echo "✅ Tests Passed"
    exit 0
else
    echo "❌ Tests Failed"
    exit 1
fi
