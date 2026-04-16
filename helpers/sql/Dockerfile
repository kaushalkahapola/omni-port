FROM eclipse-temurin:21-jdk-jammy

RUN apt-get update && apt-get install -y \
    git \
    python3 \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user matching the host uid used by run_tests.sh (1000)
RUN groupadd --gid 1000 gradle && \
    useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home gradle

RUN mkdir -p /home/gradle/.gradle && \
    echo "org.gradle.daemon=false" >> /home/gradle/.gradle/gradle.properties && \
    echo "org.gradle.parallel=true" >> /home/gradle/.gradle/gradle.properties && \
    echo "org.gradle.caching=true" >> /home/gradle/.gradle/gradle.properties && \
    chown -R gradle:gradle /home/gradle/.gradle

# /repo is mounted at runtime by run_build.sh / run_tests.sh
RUN mkdir /repo && chown gradle:gradle /repo

WORKDIR /repo
