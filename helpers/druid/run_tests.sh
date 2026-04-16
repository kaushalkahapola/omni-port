#!/bin/bash
set -e

WORKTREE_MODE="${WORKTREE_MODE:-0}"
SHORT_SHA="${COMMIT_SHA:-worktree}"
if [ "${WORKTREE_MODE}" != "1" ] && [ -n "${COMMIT_SHA:-}" ]; then
  SHORT_SHA="${COMMIT_SHA:0:7}"
fi

echo "=== Running Tests for ${SHORT_SHA} ==="
echo "Targets: ${TEST_TARGETS:-}"
echo "Modules: ${TEST_MODULES:-}"

MAX_CPU="${MAX_CPU:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 1)}"
MAVEN_THREADS="${MAVEN_THREADS:-${MAX_CPU}}"
SUREFIRE_FORKS="${SUREFIRE_FORKS:-${MAX_CPU}}"

echo "CPU detected: ${MAX_CPU}"
echo "Maven threads: ${MAVEN_THREADS}"
echo "Surefire forks: ${SUREFIRE_FORKS}"

# 1. Configure Test Command
if [ "${TEST_TARGETS:-}" == "ALL" ]; then
    # Run standard unit tests for everything (skipping broken modules)
    MAVEN_ARGS="-pl '!web-console,!distribution'"
elif [ -n "${TEST_TARGETS:-}" ] && [ "${TEST_TARGETS}" != "NONE" ]; then
    # TEST_TARGETS is a space-separated list of "module:class"
    # Example: processing:org.apache.druid.FooTest server:org.apache.druid.BarTest
    # IMPORTANT: TEST_TARGETS must take precedence over TEST_MODULES.

    MODULES=""
    TESTS=""

    # Split by space
    for target in ${TEST_TARGETS}; do
        # Split by colon
        mod="${target%%:*}"
        cls="${target#*:}"

        # Append to lists (comma separated)
        if [ -z "$MODULES" ]; then
            MODULES="$mod"
        else
            # Avoid duplicates in modules list (simple check)
            if [[ ",$MODULES," != *",$mod,"* ]]; then
                MODULES="$MODULES,$mod"
            fi
        fi

        if [ -z "$TESTS" ]; then
            TESTS="$cls"
        else
            TESTS="$TESTS,$cls"
        fi
    done

    MAVEN_ARGS="-pl ${MODULES} -am -Dtest=${TESTS}"
elif [ -n "${TEST_MODULES:-}" ]; then
    # Module-targeted fallback when no specific test classes are available.
    MAVEN_ARGS="-pl ${TEST_MODULES} -am"
elif [ "${TEST_TARGETS:-}" == "NONE" ]; then
    echo "No relevant source code changes found. Skipping tests."
    exit 0
else
    echo "No test targets/modules provided. Skipping tests."
    exit 0
fi

echo "--- Starting Test Execution ---"
echo "--- Command: mvn test -T ${MAVEN_THREADS} ${MAVEN_ARGS} -DforkCount=${SUREFIRE_FORKS} ---"

# 2. Run Tests
# We use the same 'maven-repo' volume from the build step
docker volume create maven-repo 2>/dev/null || true

# We reuse the builder image
# We mount the repo and the maven cache
# We also create a directory for aggregated results
if docker run --rm \
    -e WORKTREE_MODE="${WORKTREE_MODE}" \
    -v "${PROJECT_DIR}:/repo" \
    -v "maven-repo:/root/.m2/repository" \
    -w /repo \
    "${BUILDER_IMAGE_TAG}" \
    bash -c "if [ \"\${WORKTREE_MODE}\" != \"1\" ]; then git checkout -f ${COMMIT_SHA}; fi && \
             export MAVEN_OPTS=\"\${MAVEN_OPTS:-} -XX:ActiveProcessorCount=${MAX_CPU}\" && \
             rm -rf /repo/build/all-test-results && \
             mvn test -T ${MAVEN_THREADS} ${MAVEN_ARGS} -DforkCount=${SUREFIRE_FORKS} -DreuseForks=true -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false -Dmaven.javadoc.skip=true -Dcheckstyle.skip=true -Dpmd.skip=true -Dforbiddenapis.skip=true -Denforcer.skip=true -DskipITs; \
             MVN_EXIT_CODE=\$?; \
             mkdir -p /repo/build/all-test-results; \
             find . -name 'TEST-*.xml' -exec cp {} /repo/build/all-test-results/ \;; \
             exit \$MVN_EXIT_CODE"; then
    
    echo "✅ Tests Passed"
    exit 0
else
    echo "❌ Tests Failed"
    exit 1
fi
