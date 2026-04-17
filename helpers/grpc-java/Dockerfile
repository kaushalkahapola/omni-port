# grpc-java builder image — project files are volume-mounted at runtime.
# grpc-java uses Gradle and requires a compatible JDK (17/21 recommended for newer versions).
FROM eclipse-temurin:17-jdk-jammy

# Install base tools and build dependencies
RUN apt-get update && apt-get install -y \
    git \
    rsync \
    curl \
    wget \
    protobuf-compiler \
    libprotobuf-dev \
    libprotoc-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root 'gradle' user that matches the (usual) host UID 1000
# so file-permission issues are minimised. The helper scripts use 'chown'
# at runtime to match the actual host UID/GID if different.
RUN groupadd --gid 1000 gradle && \
    useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home gradle

# Pre-create the Gradle home so the cache volumes can be mounted there.
RUN mkdir -p /home/gradle/.gradle && \
    chown -R gradle:gradle /home/gradle/.gradle

# Global Gradle optimisations (no daemon since containers are ephemeral).
RUN echo "org.gradle.daemon=false" >> /home/gradle/.gradle/gradle.properties && \
    echo "org.gradle.parallel=true"  >> /home/gradle/.gradle/gradle.properties && \
    echo "org.gradle.caching=true"   >> /home/gradle/.gradle/gradle.properties && \
    echo "org.gradle.jvmargs=-Xmx2g -XX:+UseG1GC" >> /home/gradle/.gradle/gradle.properties

# Working directory — the live repo is mounted here at runtime.
RUN mkdir -p /repo && chown gradle:gradle /repo
WORKDIR /repo

# Add the repo directory as a "safe directory" for Git
RUN git config --global --add safe.directory /repo

CMD ["/bin/bash"]
