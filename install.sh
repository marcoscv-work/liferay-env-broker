#!/usr/bin/env bash
set -euo pipefail

TARGET_DIR="/opt/liferay-env-broker"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

sudo mkdir -p "$TARGET_DIR"
sudo rsync -av --delete "$SCRIPT_DIR/" "$TARGET_DIR/"

cd "$TARGET_DIR"
sudo python3 -m venv .venv
sudo "$TARGET_DIR/.venv/bin/pip" install --upgrade pip
sudo "$TARGET_DIR/.venv/bin/pip" install -r requirements.txt
sudo cp "$TARGET_DIR/liferay-broker.service" /etc/systemd/system/liferay-broker.service
sudo systemctl daemon-reload
sudo systemctl enable --now liferay-broker.service

echo "Installed. Review $TARGET_DIR/config.yaml and replace placeholder tokens before shared use."
echo "UI available at: http://BROKER_HOST:8899/ui"
