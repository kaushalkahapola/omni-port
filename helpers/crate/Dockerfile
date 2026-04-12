FROM maven:3.9-eclipse-temurin-24

# Install necessary tools
RUN apt-get update && apt-get install -y \
    git \
    rsync \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Install additional JDKs
# JDK 24 is already at /opt/java/openjdk (from base image)
RUN mkdir -p /opt/java && \
    wget -qO- https://github.com/adoptium/temurin22-binaries/releases/download/jdk-22.0.2%2B9/OpenJDK22U-jdk_x64_linux_hotspot_22.0.2_9.tar.gz | tar -xz -C /opt/java && \
    wget -qO- https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.4%2B7/OpenJDK21U-jdk_x64_linux_hotspot_21.0.4_7.tar.gz | tar -xz -C /opt/java && \
    wget -qO- https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.12%2B7/OpenJDK17U-jdk_x64_linux_hotspot_17.0.12_7.tar.gz | tar -xz -C /opt/java

# Set working directory
WORKDIR /repo

# Set environment variables
ENV MAVEN_OPTS="-Xmx4g -XX:+UseG1GC"

# Default command
CMD ["/bin/bash"]
