#!/bin/bash

# Function to check if a command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

echo "Starting setup for OmniPort project..."

# Update package lists and install basic dependencies
echo "Updating package lists..."
sudo apt-get update
sudo apt-get install -y curl wget git build-essential software-properties-common

# 1. Install/Check Python 3.12
echo "Checking for Python 3.12..."
if ! command_exists python3.12; then
    echo "Python 3.12 not found. Installing from deadsnakes PPA..."
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update
    sudo apt-get install -y python3.12 python3.12-venv python3.12-dev
else
    echo "Python 3.12 is already installed."
fi

# 2. Install/Check uv
echo "Checking for uv..."
if ! command_exists uv; then
    echo "uv not found. Installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Add uv to PATH for the current script session
    export PATH="$HOME/.cargo/bin:$PATH"
    # Ensure it's in the user's shell profile for future sessions
    if [[ ":$PATH:" != *":$HOME/.cargo/bin:"* ]]; then
        echo 'export PATH="$HOME/.cargo/bin:$PATH"' >> ~/.bashrc
    fi
else
    echo "uv is already installed."
fi

# 3. Install/Check Java 17
echo "Checking for Java 17..."
if command_exists java; then
    JAVA_VER=$(java -version 2>&1 | head -n 1 | awk -F '"' '{print $2}' | cut -d'.' -f1)
else
    JAVA_VER="none"
fi

if [[ "$JAVA_VER" != "17" ]]; then
    echo "Java 17 not found (current: $JAVA_VER). Installing OpenJDK 17..."
    sudo apt-get install -y openjdk-17-jdk
else
    echo "Java 17 is already installed."
fi

# 4. Install/Check Maven
echo "Checking for Maven..."
if ! command_exists mvn; then
    echo "Maven not found. Installing..."
    sudo apt-get install -y maven
else
    echo "Maven is already installed."
fi

# 5. Install/Check Docker
echo "Checking for Docker..."
if ! command_exists docker; then
    echo "Docker not found. Installing via official script..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker $USER
    rm get-docker.sh
    echo "Docker installed. Note: You may need to log out and back in for group changes to take effect."
else
    echo "Docker is already installed."
fi

# 6. Set up Python environment with uv
if [ -f "pyproject.toml" ]; then
    echo "Setting up Python environment with uv..."
    # Use uv to create a venv with Python 3.12
    uv venv --python 3.12
    # Activate and sync dependencies
    . .venv/bin/activate
    uv sync
else
    echo "Warning: pyproject.toml not found. Skipping uv sync."
fi

# 7. Build Java microservice
if [ -d "java-microservice" ]; then
    echo "Building Java microservice..."
    cd java-microservice
    # Ensure we use the right Java version for the build if multiple are present
    export JAVA_HOME=$(readlink -f /usr/bin/java | sed "s:bin/java::")
    mvn clean install -DskipTests
    cd ..
else
    echo "Warning: java-microservice directory not found. Skipping build."
fi

echo "--------------------------------------------------"
echo "Setup complete!"
echo "Please run 'source .venv/bin/activate' to enter the Python environment."
echo "If this is your first time installing Docker, remember to log out and back in."
echo "--------------------------------------------------"
