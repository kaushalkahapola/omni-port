#!/bin/bash
set -e

echo "=== Running Tests for ${COMMIT_SHA:0:7} ==="
echo "Target: ${TEST_TARGETS}"

IMAGE_TAG="${BUILDER_IMAGE_TAG:-${IMAGE_TAG:-retrofit-hadoop-builder:local}}"

if [ "${TEST_TARGETS}" == "ALL" ]; then
    MAVEN_ARGS="-pl '!hadoop-yarn-project/hadoop-yarn/hadoop-yarn-applications/hadoop-yarn-applications-catalog/hadoop-yarn-applications-catalog-webapp'"
elif [ "${TEST_TARGETS}" == "NONE" ] || [ -z "${TEST_TARGETS}" ]; then
    echo "No relevant source code changes found. Skipping tests."
    exit 0
elif [[ "${TEST_TARGETS}" == *":"* ]]; then
    MODULES=""
    CLASSES=""
    for target in ${TEST_TARGETS}; do
        if [[ "$target" == *":"* ]]; then
            MOD="${target%%:*}"
            CLS="${target#*:}"
            if [ -z "$MODULES" ]; then MODULES="$MOD"
            elif [[ ",$MODULES," != *",$MOD,"* ]]; then MODULES="$MODULES,$MOD"; fi
            if [ -z "$CLASSES" ]; then CLASSES="$CLS"
            else CLASSES="$CLASSES,$CLS"; fi
        fi
    done
    MAVEN_ARGS="-pl ${MODULES} -Dtest=${CLASSES} -am -DfailIfNoTests=false"
else
    COMMA_TARGETS=$(echo "${TEST_TARGETS}" | tr ' ' ',')
    MAVEN_ARGS="-pl ${COMMA_TARGETS} -am"
fi

DOCKER_CMD="docker"
if ! docker info > /dev/null 2>&1; then
    if sudo docker info > /dev/null 2>&1; then DOCKER_CMD="sudo docker"; fi
fi

HELPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

${DOCKER_CMD} volume create maven-cache-hadoop 2>/dev/null || true

echo "--- Starting Test Execution: mvn test ${MAVEN_ARGS} ---"

if ${DOCKER_CMD} run --rm \
    --dns=8.8.8.8 \
    -v "maven-cache-hadoop:/root/.m2" \
    -v "${PROJECT_DIR}:/repo" \
    -v "${HELPER_DIR}/settings.xml:/root/.m2/settings.xml:ro" \
    -w /repo \
    "${IMAGE_TAG}" \
    bash -c "git config --global --add safe.directory /repo; \
             set +e; \
             mvn test ${MAVEN_ARGS} \
                 -DfailIfNoTests=false \
                 -Dsurefire.failIfNoSpecifiedTests=false \
                 -Dmaven.javadoc.skip=true \
                 -Drat.skip=true \
                 -Dcheckstyle.skip=true \
                 -Denforcer.skip=true \
                 -Dmaven.wagon.http.retryHandler.count=3 \
                 -Dmaven.wagon.httpconnectionManager.ttlSeconds=120 \
                 --no-transfer-progress; \
             MVN_EXIT_CODE=\$?; \
             set -e; \
             mkdir -p /repo/build/all-test-results; \
             find /repo -path '*/target/surefire-reports/TEST-*.xml' -exec cp {} /repo/build/all-test-results/ \; 2>/dev/null || true; \
             echo \"Copied \$(ls /repo/build/all-test-results/*.xml 2>/dev/null | wc -l) XML files\"; \
             exit \$MVN_EXIT_CODE"; then
    TEST_OK=true
else
    TEST_OK=false
fi

# Fix permissions back to host user AFTER the test run so git restore works
${DOCKER_CMD} run --rm -u root \
    -v "${PROJECT_DIR}:/repo" \
    "${IMAGE_TAG}" \
    bash -c "chown -R 1000:1000 /repo 2>/dev/null || true"

if [ "$TEST_OK" = "true" ]; then
    echo "✅ Tests Passed"
    exit 0
else
    echo "❌ Tests Failed"
    exit 1
fi
