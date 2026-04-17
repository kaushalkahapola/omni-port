#!/bin/bash
# Builds (compiles) the Hadoop project inside Docker.
set -e

echo "--- Building Hadoop for ${COMMIT_SHA:0:7} ---"
cd "${PROJECT_DIR}"
git config --global --add safe.directory "${PROJECT_DIR}"

# Only checkout when NOT in worktree mode.
if [ "${WORKTREE_MODE}" != "1" ]; then
    echo "--- Checking out commit... ---"
    git checkout -f ${COMMIT_SHA}
else
    echo "--- Worktree mode: skipping git checkout ---"
fi

BUILDER_IMAGE="${BUILDER_IMAGE_TAG:-${IMAGE_TAG:-retrofit-hadoop-builder:local}}"
HELPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DOCKER_CMD="docker"
if ! docker info > /dev/null 2>&1; then
    if sudo docker info > /dev/null 2>&1; then DOCKER_CMD="sudo docker"; fi
fi

${DOCKER_CMD} volume create maven-cache-hadoop 2>/dev/null || true

# Fix permissions before build AND create dummy test-jars for ALL SNAPSHOT versions
# that don't exist in any public repo (prevents cached dependency resolution failures)
${DOCKER_CMD} run --rm -u root \
    -v "maven-cache-hadoop:/root/.m2" \
    -v "${PROJECT_DIR}:/repo" \
    ${BUILDER_IMAGE} \
    bash -c "
chown -R root:root /root/.m2 /repo 2>/dev/null || true
find /root/.m2/repository -name '*.lastUpdated' -delete 2>/dev/null || true
find /root/.m2/repository -name '_remote.repositories' -delete 2>/dev/null || true

# Create dummy test-jars for all known Hadoop artifacts across all SNAPSHOT versions.
# These test-jars are never published to Maven Central but are declared as test-scoped
# dependencies — Maven tries to resolve them and fails without this workaround.
for version in 3.3.9-SNAPSHOT 3.4.1-SNAPSHOT 3.4.2-SNAPSHOT 3.3.6-SNAPSHOT 3.3.8-SNAPSHOT; do
  for artifact in \
    hadoop-auth hadoop-common hadoop-hdfs hadoop-hdfs-nfs hadoop-hdfs-httpfs \
    hadoop-yarn hadoop-yarn-common hadoop-yarn-client hadoop-yarn-registry \
    hadoop-yarn-server hadoop-yarn-server-common hadoop-yarn-server-tests \
    hadoop-yarn-server-nodemanager hadoop-yarn-server-resourcemanager \
    hadoop-yarn-server-router hadoop-yarn-server-globalpolicygenerator \
    hadoop-yarn-server-timelineservice hadoop-yarn-server-timelineservice-hbase \
    hadoop-yarn-server-timelineservice-hbase-server \
    hadoop-yarn-server-timelineservice-hbase-server-2 \
    hadoop-yarn-applications hadoop-yarn-applications-distributedshell \
    hadoop-yarn-applications-unmanaged-am-launcher \
    hadoop-yarn-csi hadoop-yarn-services hadoop-yarn-ui \
    hadoop-mapreduce-client hadoop-mapreduce-client-common \
    hadoop-mapreduce-client-shuffle hadoop-mapreduce-client-hs \
    hadoop-mapreduce-client-app \
    hadoop-kms hadoop-nfs hadoop-minicluster hadoop-minikdc \
    hadoop-annotations hadoop-build-tools hadoop-maven-plugins \
    hadoop-auth-examples hadoop-assemblies hadoop-main \
    hadoop-project hadoop-project-dist hadoop-common-project \
    hadoop-hdfs-project hadoop-tools; do
    dir=\"/root/.m2/repository/org/apache/hadoop/\${artifact}/\${version}\"
    jar=\"\${dir}/\${artifact}-\${version}-tests.jar\"
    mkdir -p \"\${dir}\"
    if [ ! -f \"\${jar}\" ]; then
      printf 'PK\x05\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00' > \"\${jar}\"
    fi
  done
done
# Also cover any other SNAPSHOT dirs already in the cache
find /root/.m2/repository/org/apache/hadoop -type d -name '*-SNAPSHOT' | while read dir; do
  artifact=\$(basename \$(dirname \$dir))
  version=\$(basename \$dir)
  jar=\"\${dir}/\${artifact}-\${version}-tests.jar\"
  if [ ! -f \"\${jar}\" ]; then
    printf 'PK\x05\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00' > \"\${jar}\"
  fi
done
"

# Determine which modules to build from TEST_MODULES or TEST_TARGETS env vars.
# Default to hadoop-common-project/hadoop-common if nothing is specified.
BUILD_MODULES="${SOURCE_MODULES:-}"
if [ -z "${BUILD_MODULES}" ] && [ -n "${TEST_MODULES}" ]; then
    BUILD_MODULES="${TEST_MODULES}"
fi
if [ -z "${BUILD_MODULES}" ] && [ -n "${TEST_TARGETS}" ] && [ "${TEST_TARGETS}" != "NONE" ] && [ "${TEST_TARGETS}" != "ALL" ]; then
    # Extract module part from "module:class" targets
    BUILD_MODULES=$(echo "${TEST_TARGETS}" | tr ' ' '\n' | sed 's/:.*//' | sort -u | tr '\n' ',' | sed 's/,$//')
fi
if [ -z "${BUILD_MODULES}" ]; then
    BUILD_MODULES="hadoop-common-project/hadoop-common"
fi

echo "--- Building modules: ${BUILD_MODULES} ---"

MVN_CMD="mvn compile \
    -Dmaven.javadoc.skip=true \
    -Drat.skip=true \
    -Dcheckstyle.skip=true \
    -Denforcer.skip=true \
    --no-transfer-progress \
    -Dmaven.wagon.http.retryHandler.count=3 \
    -pl ${BUILD_MODULES} \
    -am"

echo "--- Compiling with Maven ---"
if ${DOCKER_CMD} run --rm \
    --dns=8.8.8.8 \
    -v "maven-cache-hadoop:/root/.m2" \
    -v "${PROJECT_DIR}:/repo" \
    -v "${HELPER_DIR}/settings.xml:/root/.m2/settings.xml:ro" \
    -w /repo \
    ${BUILDER_IMAGE} \
    bash -c "git config --global --add safe.directory /repo; ${MVN_CMD}"; then
    BUILD_OK=true
else
    BUILD_OK=false
fi

# Fix permissions back to host user AFTER the build so git operations work
${DOCKER_CMD} run --rm -u root \
    -v "${PROJECT_DIR}:/repo" \
    ${BUILDER_IMAGE} \
    bash -c "chown -R 1000:1000 /repo 2>/dev/null || true"

if [ "$BUILD_OK" = "true" ]; then
    echo "--- Build complete for ${COMMIT_SHA:0:7} ---"
    exit 0
else
    echo "--- Build FAILED for ${COMMIT_SHA:0:7} ---"
    exit 1
fi
