ARG JAVA_VERSION=21
# Use a standard, trusted Java (JDK) image. Hibernate 6.x/7.x usually needs newer JDK.
FROM eclipse-temurin:${JAVA_VERSION}-jdk-jammy

# Install basic tools
RUN apt-get update && apt-get install -y \
    git \
    python3 \
    unzip \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install OpenJDK 25.0.1
RUN curl -L "https://download.java.net/java/GA/jdk25.0.1/2fbf10d8c78e40bd87641c434705079d/8/GPL/openjdk-25.0.1_linux-x64_bin.tar.gz" -o /tmp/jdk25.tar.gz && \
    mkdir -p /opt/java && \
    tar -xzf /tmp/jdk25.tar.gz -C /opt/java && \
    mv /opt/java/jdk-25.0.1 /opt/java/jdk-25 && \
    rm /tmp/jdk25.tar.gz

ENV JDK25_HOME=/opt/java/jdk-25

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
