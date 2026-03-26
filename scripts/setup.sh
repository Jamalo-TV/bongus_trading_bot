#!/bin/bash

set -e

echo "Starting server provisioning for Bongus Trading Bot..."

# Update package lists
echo "Updating package lists..."
sudo apt-get update -y

# Install build dependencies required by Rust, Python, and OpenSSL
echo "Installing build essentials, libssl-dev, and pkg-config..."
sudo apt-get install -y build-essential libssl-dev pkg-config python3-venv python3-pip git curl

# Install Rust and Cargo if not already installed
if ! command -v cargo &> /dev/null
then
    echo "Rust/Cargo not found. Installing Rust..."
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    # Ensure cargo is available in current shell session
    source "$HOME/.cargo/env"
else
    echo "Rust/Cargo is already installed."
fi

# Set up the Python Virtual Environment
echo "Setting up Python virtual environment..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

# Activate the virtual environment
source .venv/bin/activate

# Install Python dependencies
echo "Installing Python dependencies..."
pip install --upgrade pip
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
else
    echo "requirements.txt not found, skipping Python dependencies installation."
fi

# Build the Rust Execution Engine
echo "Building Rust Execution Engine..."
if [ -d "execution_engine" ]; then
    cd execution_engine
    cargo build --release
    cd ..
else
    echo "execution_engine directory not found, skipping Rust build."
fi

echo "Setup complete! Please configure your environment variables (BINANCE_API_KEY, BINANCE_API_SECRET) before running the bot."
