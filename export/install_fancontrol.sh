#!/bin/bash
set -e

INSTALL_DIR="/volume1/docker/fancontrol-web"
REPO="https://github.com/Biowolfx/fancontrol-web.git"

echo "=== FanControl Web Installer ==="

# Clone or pull
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Updating existing installation..."
    cd "$INSTALL_DIR"
    git pull origin main
else
    echo "Cloning repository..."
    git clone "$REPO" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# Build and start
GIT_HASH=$(git rev-parse --short HEAD)
echo "Building with GIT_HASH=$GIT_HASH..."
docker compose build --build-arg GIT_HASH="$GIT_HASH"
docker compose up -d

echo ""
echo "=== Done! ==="
echo "Access at: http://$(hostname -I | awk '{print $1}'):5059"
echo "To update later: cd $INSTALL_DIR && git pull && docker compose build --build-arg GIT_HASH=\$(git rev-parse --short HEAD) && docker compose up -d"
