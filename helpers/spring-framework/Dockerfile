ARG JAVA_VERSION=17
# Use a standard, trusted Java (JDK) image
FROM eclipse-temurin:${JAVA_VERSION}-jdk-jammy

# Install basic tools
RUN apt-get update && apt-get install -y \
    git \
    python3 \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user 'gradle'
RUN groupadd --gid 1000 gradle && \
    useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home gradle

# Create the global .gradle directory
RUN mkdir -p /home/gradle/.gradle && \
    chown -R gradle:gradle /home/gradle/.gradle

# Create gradle.properties with optimization settings and build scan disabled
RUN echo "org.gradle.daemon=true" >> /home/gradle/.gradle/gradle.properties && \
    echo "org.gradle.parallel=true" >> /home/gradle/.gradle/gradle.properties && \
    echo "org.gradle.caching=true" >> /home/gradle/.gradle/gradle.properties && \
    echo "org.gradle.jvmargs=-Xmx4g" >> /home/gradle/.gradle/gradle.properties && \
    echo "org.gradle.console=plain" >> /home/gradle/.gradle/gradle.properties && \
    chown gradle:gradle /home/gradle/.gradle/gradle.properties

# Create an init script to disable build scans
RUN mkdir -p /home/gradle/.gradle/init.d && \
    echo "gradle.rootProject { root ->" >> /home/gradle/.gradle/init.d/disable-scans.gradle && \
    echo "  root.extensions.findByName('buildScan')?.publishOnFailure()" >> /home/gradle/.gradle/init.d/disable-scans.gradle && \
    echo "  root.extensions.findByName('buildScan')?.publishAlways()" >> /home/gradle/.gradle/init.d/disable-scans.gradle && \
    echo "}" >> /home/gradle/.gradle/init.d/disable-scans.gradle && \
    chown -R gradle:gradle /home/gradle/.gradle/init.d

# Set up working directory
RUN mkdir -p /repo && chown gradle:gradle /repo
WORKDIR /repo

# Build will be done by mounting the repo volume