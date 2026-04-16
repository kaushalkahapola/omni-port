# Use a standard, trusted Java 17 (JDK) image
FROM eclipse-temurin:17-jdk-jammy

# Install basic tools (git, python3) needed for build scripts
RUN apt-get update && apt-get install -y \
    git \
    python3 \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user 'gradle'
RUN groupadd --gid 1000 gradle && \
    useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home gradle

# Create the global .gradle directory
RUN mkdir -p /home/gradle/.gradle

# Create our global gradle.properties file with optimization settings
RUN echo "org.gradle.daemon=true" >> /home/gradle/.gradle/gradle.properties && \
    echo "org.gradle.parallel=true" >> /home/gradle/.gradle/gradle.properties && \
    echo "org.gradle.caching=true" >> /home/gradle/.gradle/gradle.properties

# Set ownership for the .gradle dir
RUN chown -R gradle:gradle /home/gradle/.gradle

# Create the /repo dir and give the gradle user ownership
RUN mkdir /repo
RUN chown gradle:gradle /repo

# Set up a working directory inside the container
WORKDIR /repo

# Note: We do NOT copy source code here to allow incremental builds via volume mounting.