#!/bin/bash
set -e

echo "=== Running Tests for ${COMMIT_SHA:0:7} ==="
echo "Target: ${TEST_TARGETS:-}"
echo "Modules: ${TEST_MODULES:-}"

MAX_CPU="${MAX_CPU:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 1)}"
MAVEN_THREADS="${MAVEN_THREADS:-${MAX_CPU}}"
SUREFIRE_FORKS="${SUREFIRE_FORKS:-${MAX_CPU}}"

echo "CPU detected: ${MAX_CPU}"
echo "Maven threads: ${MAVEN_THREADS}"
echo "Surefire forks: ${SUREFIRE_FORKS}"

# 1. Configure Test Command
if [ "${TEST_TARGETS:-}" == "ALL" ]; then
    echo "--- Running ALL tests (excluding blacklisted modules) ---"
    MAVEN_ARGS="-pl '!hbase-assembly,!hbase-archetypes'"
elif [ -n "${TEST_TARGETS:-}" ] && [ "${TEST_TARGETS}" != "NONE" ]; then
    # Check if we have granular targets (contain ':')
    if [[ "${TEST_TARGETS}" == *":"* ]]; then
        echo "--- Granular Test Mode: Running specific test classes ---"
        
        MODULES=""
        CLASSES=""
        
        # Parse targets
        for target in ${TEST_TARGETS}; do
            if [[ "$target" == *":"* ]]; then
                MOD="${target%%:*}"
                CLS="${target#*:}"
                
                # Add to comma-separated lists
                if [ -z "$MODULES" ]; then
                    MODULES="$MOD"
                else
                    # Check if module already in list
                    if [[ ",$MODULES," != *",$MOD,"* ]]; then
                        MODULES="$MODULES,$MOD"
                    fi
                fi
                
                if [ -z "$CLASSES" ]; then
                    CLASSES="$CLS"
                else
                    CLASSES="$CLASSES,$CLS"
                fi
            fi
        done
        
        if [ -z "$CLASSES" ]; then
            echo "Error: No valid test classes found"
            exit 1
        fi
        
        echo "Modules: $MODULES"
        echo "Test Classes: $CLASSES"
        echo "Number of test classes: $(echo $CLASSES | tr ',' '\n' | wc -l)"
        
        # Use -Dtest to run specific tests, -am to build dependencies
        MAVEN_ARGS="-pl ${MODULES} -Dtest=${CLASSES} -am -DfailIfNoTests=false"
    else
        echo "--- Module Test Mode: Running all tests in affected modules ---"
        
        # Convert space-separated to comma-separated
        COMMA_TARGETS=$(echo "${TEST_TARGETS}" | tr ' ' ',')
        
        echo "Affected Modules: $COMMA_TARGETS"
        
        # Run all tests in specified modules, build dependencies
        MAVEN_ARGS="-pl ${COMMA_TARGETS} -am"
    fi
elif [ -n "${TEST_MODULES:-}" ]; then
    echo "--- Module Fallback Mode: Running all tests in TEST_MODULES ---"
    MAVEN_ARGS="-pl ${TEST_MODULES} -am"
elif [ "${TEST_TARGETS:-}" == "NONE" ]; then
    echo "No relevant source code changes found. Skipping tests."
    exit 0
else
    echo "No TEST_TARGETS or TEST_MODULES set. Skipping tests."
    exit 0
fi

echo "--- Starting Test Execution ---"
echo "--- Maven Args: ${MAVEN_ARGS} ---"

# 2. Create Maven cache volume
docker volume create maven-cache-hbase 2>/dev/null || true

# 3. Run Tests in Docker
if docker run --rm \
    --dns=8.8.8.8 \
    -v "${PROJECT_DIR}:/repo" \
    -v "maven-cache-hbase:/root/.m2" \
    -w /repo \
    "${BUILDER_IMAGE_TAG}" \
    bash -c "set -e; \
             echo 'Maven version:'; mvn --version; \
             export MAVEN_OPTS=\"\${MAVEN_OPTS:-} -XX:ActiveProcessorCount=${MAX_CPU}\"; \
             echo 'Running: mvn test -T ${MAVEN_THREADS} -DforkCount=${SUREFIRE_FORKS} ${MAVEN_ARGS}'; \
             mvn test -T ${MAVEN_THREADS} -DforkCount=${SUREFIRE_FORKS} -DreuseForks=true ${MAVEN_ARGS} \
                  -DfailIfNoTests=false \
                  -Dsurefire.failIfNoSpecifiedTests=false \
                  -Dmaven.javadoc.skip=true \
                  -Dcheckstyle.skip=true \
                  -Dfindbugs.skip=true \
                 -Dspotbugs.skip=true \
                 -Denforcer.skip=true; \
             MVN_EXIT_CODE=\$?; \
             echo 'Collecting test results...'; \
             mkdir -p /repo/build/all-test-results; \
             find . -path '*/target/surefire-reports/*.xml' -exec cp {} /repo/build/all-test-results/ \; 2>/dev/null || true; \
             echo \"Found \$(ls /repo/build/all-test-results/*.xml 2>/dev/null | wc -l) test result files\"; \
             exit \$MVN_EXIT_CODE"; then
    
    echo "✅ Tests Passed"
    exit 0
else
    echo "❌ Tests Failed"
    exit 1
fi
