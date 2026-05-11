#!/usr/bin/env bash
# Deploy the arb-engine to your Oracle Cloud VPS.
# Idempotent: safe to re-run. Creates/updates systemd unit + env file.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/poly}"
SERVICE_NAME="${SERVICE_NAME:-arb-engine}"
PY_BIN="${PY_BIN:-python3}"

if [[ ! -d "$REPO_DIR" ]]; then
  echo "cloning into $REPO_DIR"
  git clone https://github.com/jt8530-beep/poly.git "$REPO_DIR"
fi

cd "$REPO_DIR"
git fetch --all
git checkout arb-engine-v0
git pull --ff-only origin arb-engine-v0

# deps
$PY_BIN -m pip install --user --upgrade pip
$PY_BIN -m pip install --user requests   # reserved; stdlib urllib used now

# env file (put your secrets here, then `chmod 600`)
ENV_FILE="$HOME/.config/arb-engine.env"
mkdir -p "$(dirname "$ENV_FILE")"
if [[ ! -f "$ENV_FILE" ]]; then
cat >"$ENV_FILE" <<EOF
PAPER_MODE=true
SCAN_INTERVAL_SEC=30
LADDER_MIN_VIOLATION_BPS=200
LADDER_MIN_DEPTH_USD=20
RISK_MAX_NOTIONAL_PER_TRADE=30
RISK_MAX_OPEN_NOTIONAL=200
RISK_MAX_DAILY_NEW_TRADES=20
RISK_HARD_DD=0.35
RISK_SOFT_DD=0.20
TG_BOT_TOKEN=
TG_CHAT_ID=
DB_PATH=$HOME/.local/share/arb-engine/ledger.sqlite
LOG_LEVEL=INFO
EOF
  chmod 600 "$ENV_FILE"
  echo "created env template at $ENV_FILE - fill in TG_BOT_TOKEN and TG_CHAT_ID"
fi
mkdir -p "$HOME/.local/share/arb-engine"

# systemd unit (user-mode, no root required on Oracle Linux / Ubuntu)
UNIT="$HOME/.config/systemd/user/${SERVICE_NAME}.service"
mkdir -p "$(dirname "$UNIT")"
cat >"$UNIT" <<EOF
[Unit]
Description=Polymarket arb engine (ladder C)
After=network-online.target

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
WorkingDirectory=$REPO_DIR
ExecStart=$(command -v $PY_BIN) -m arb.main
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "${SERVICE_NAME}.service"
systemctl --user status "${SERVICE_NAME}.service" --no-pager | head -20
echo
echo "Tail logs with:  journalctl --user -u ${SERVICE_NAME} -f"
