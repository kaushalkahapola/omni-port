#!/bin/bash
set -e # Exit on error

WORKTREE_MODE="${WORKTREE_MODE:-0}"
echo "--- Building code for ${COMMIT_SHA:0:7} --- (WORKTREE_MODE=${WORKTREE_MODE})"

# 1. Checkout commit
echo "--- Changing directory to ${PROJECT_DIR} ---"
cd "${PROJECT_DIR}"

if [ "${WORKTREE_MODE}" != "1" ]; then
    echo "--- Checking out commit... ---"
    git checkout -f ${COMMIT_SHA}
fi

# 2. Build Docker image
echo "--- Building Docker image... ---"
# Use TOOLKIT_DIR as context to avoid permission errors in target/ folders
# If TOOLKIT_DIR is not set, use the directory of this script.
HELPER_DIR="${TOOLKIT_DIR:-$(dirname "$0")}"
docker build -t ${BUILDER_IMAGE_TAG} -f ${HELPER_DIR}/Dockerfile ${HELPER_DIR}

# 3. Run Build
echo "--- Running Maven Build... ---"

docker volume create maven-repo 2>/dev/null || true

# Run build
docker run --rm \
    -v "${PROJECT_DIR}:/repo" \
    -v "maven-repo:/root/.m2/repository" \
    -w /repo \
    ${BUILDER_IMAGE_TAG} \
    bash -c "mvn clean install -DskipTests -Dskip.yarn -Dskip.npm -Dskip.installnodenpm -Dmaven.antrun.skip=true -Dmaven.javadoc.skip=true -Dcheckstyle.skip=true -Dpmd.skip=true -Dforbiddenapis.skip=true -Denforcer.skip=true -Drat.skip=true -T 1C" \
    || BUILD_EXIT_CODE=$?

# Save build status
BUILD_EXIT_CODE=${BUILD_EXIT_CODE:-0}
if [ -n "${BUILD_STATUS_FILE:-}" ]; then
    if [ "${BUILD_EXIT_CODE}" -eq 0 ]; then
        echo "Success" > "${BUILD_STATUS_FILE}"
    else
        echo "Fail" > "${BUILD_STATUS_FILE}"
    fi
fi

echo "--- Build complete for ${COMMIT_SHA:0:7} ---"
exit ${BUILD_EXIT_CODE}
