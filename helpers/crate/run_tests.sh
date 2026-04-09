#!/bin/bash
set -e

echo "=== Running Tests for ${COMMIT_SHA:0:7} ==="
echo "Target: ${TEST_TARGETS}"
# BUILD_DIR must be outside PROJECT_DIR to avoid recursive Docker build context
BUILD_DIR="${BUILD_DIR:-/tmp/crate-build-${COMMIT_SHA:0:7}}"
mkdir -p "${BUILD_DIR}"

IMAGE_TAG="${IMAGE_TAG:-crate-${BUILD_TYPE}-${COMMIT_SHA:0:7}}"

MAX_CPU="${MAX_CPU:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 1)}"
MAVEN_THREADS="${MAVEN_THREADS:-${MAX_CPU}}"
SUREFIRE_FORKS="${SUREFIRE_FORKS:-${MAX_CPU}}"

echo "--- Using Docker Image: ${IMAGE_TAG} ---"
echo "CPU detected: ${MAX_CPU}"
echo "Maven threads: ${MAVEN_THREADS}"
echo "Surefire forks: ${SUREFIRE_FORKS}"

# 1. Configure Test Command
if [ "${TEST_TARGETS:-}" == "ALL" ]; then
    MAVEN_ARGS=""
elif [ -n "${TEST_TARGETS:-}" ] && [ "${TEST_TARGETS}" != "NONE" ]; then
    # TEST_TARGETS is a space-separated list of "module:class"
    MODULES=""
    TESTS=""
    for target in ${TEST_TARGETS}; do
        mod="${target%%:*}"
        cls="${target#*:}"
        if [ -z "$MODULES" ]; then MODULES="$mod"; else
            if [[ ",$MODULES," != *",$mod,"* ]]; then MODULES="$MODULES,$mod"; fi
        fi
        if [ -z "$TESTS" ]; then TESTS="$cls"; else TESTS="$TESTS,$cls"; fi
    done
    MAVEN_ARGS="-pl ${MODULES} -am -Dtest=${TESTS}"
elif [ -n "${TEST_MODULES:-}" ]; then
    # Module-targeted fallback
    MAVEN_ARGS="-pl ${TEST_MODULES} -am"
elif [ "${TEST_TARGETS:-}" == "NONE" ]; then
    echo "No relevant source code changes found. Skipping tests."
    exit 0
else
    echo "No TEST_TARGETS or TEST_MODULES set. Skipping tests."
    exit 0
fi

MVN_CMD="mvn test -T ${MAVEN_THREADS} ${MAVEN_ARGS} -DforkCount=${SUREFIRE_FORKS} -DreuseForks=true -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false -Dmaven.javadoc.skip=true -Dcheckstyle.skip=true -Dpmd.skip=true -Dforbiddenapis.skip=true -Denforcer.skip=true -DskipITs"

DOCKER_CMD="docker"
if ! docker info > /dev/null 2>&1; then
    if sudo docker info > /dev/null 2>&1; then
        echo "Docker requires sudo. Using 'sudo docker'."
        DOCKER_CMD="sudo docker"
    else
        echo "Warning: Docker command failed. Continuing with 'docker' but expect errors."
    fi
fi

${DOCKER_CMD} volume create maven-cache-crate 2>/dev/null || true

echo "--- Executing: ${MVN_CMD} ---"

if ${DOCKER_CMD} run --rm \
    --dns=8.8.8.8 \
    -v "maven-cache-crate:/root/.m2" \
    -v "${PROJECT_DIR}:/repo" \
    -w /repo \
    "${IMAGE_TAG}" \
    bash -c "git config --global --add safe.directory /repo && \
    export MAVEN_OPTS=\"\${MAVEN_OPTS:-} -XX:ActiveProcessorCount=${MAX_CPU}\" && \
    mkdir -p /root/.m2 && \
    echo '<toolchains><toolchain><type>jdk</type><provides><version>24.0.2</version><vendor>temurin</vendor></provides><configuration><jdkHome>/opt/java/openjdk</jdkHome></configuration></toolchain></toolchains>' > /root/.m2/toolchains.xml && \
    ${MVN_CMD} --global-toolchains /root/.m2/toolchains.xml; \
    MVN_EXIT_CODE=\$?; \
    echo '--- Test results are available in /repo/*/target/surefire-reports/ ---'; \
    exit \$MVN_EXIT_CODE"; then
    
    echo "✅ Tests Passed"
    exit 0
else
    echo "❌ Tests Failed"
    exit 1
fi
