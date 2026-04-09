# Elasticsearch builder image — project files are volume-mounted at runtime.
# Elasticsearch uses Gradle and requires a compatible JDK (17+).
FROM eclipse-temurin:21-jdk-jammy

# Install git and rsync; both are needed by the helper scripts.
RUN apt-get update && apt-get install -y \
    git \
    rsync \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root 'gradle' user that matches the UID Elasticsearch's own
# Gradle wrapper expects (UID 1000) so file-permission issues are minimised.
RUN groupadd --gid 1000 gradle && \
    useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home gradle

# Pre-create the Gradle home so the cache volumes can be mounted there.
RUN mkdir -p /home/gradle/.gradle && \
    chown -R gradle:gradle /home/gradle/.gradle

# Global Gradle optimisations (no daemon since containers are ephemeral).
RUN echo "org.gradle.daemon=false" >> /home/gradle/.gradle/gradle.properties && \
    echo "org.gradle.parallel=true"  >> /home/gradle/.gradle/gradle.properties && \
    echo "org.gradle.caching=true"   >> /home/gradle/.gradle/gradle.properties && \
    echo "org.gradle.jvmargs=-Xmx4g -XX:+UseG1GC" >> /home/gradle/.gradle/gradle.properties

# Working directory — the live repo is mounted here at runtime.
RUN mkdir -p /repo && chown gradle:gradle /repo
WORKDIR /repo

CMD ["/bin/bash"]