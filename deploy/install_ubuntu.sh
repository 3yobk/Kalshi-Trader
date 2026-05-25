#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/weather-bot}"
SERVICE_USER="${SERVICE_USER:-weatherbot}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "Installing weather bot into ${APP_DIR}"
echo "This script prepares paper/read-only operation. It does not enable live trading."

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root with sudo."
  exit 1
fi

id "${SERVICE_USER}" >/dev/null 2>&1 || useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
mkdir -p "${APP_DIR}" "${APP_DIR}/data" "${APP_DIR}/exports"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"

if [[ ! -f "${APP_DIR}/requirements.txt" ]]; then
  echo "Copy the project files into ${APP_DIR} before running this script."
  exit 1
fi

sudo -u "${SERVICE_USER}" "${PYTHON_BIN}" -m venv "${APP_DIR}/.venv"
sudo -u "${SERVICE_USER}" "${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
sudo -u "${SERVICE_USER}" "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

if [[ ! -f "${APP_DIR}/.env" ]]; then
  cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  chown "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}/.env"
  chmod 600 "${APP_DIR}/.env"
  echo "Created ${APP_DIR}/.env from example. Fill in Kalshi/API keys before starting services."
fi

install -m 0644 "${APP_DIR}/deploy/weather-bot.service.example" /etc/systemd/system/weather-bot.service
install -m 0644 "${APP_DIR}/deploy/weather-bot-monitor.service.example" /etc/systemd/system/weather-bot-monitor.service
install -m 0644 "${APP_DIR}/deploy/weather-bot-monitor.timer.example" /etc/systemd/system/weather-bot-monitor.timer

systemctl daemon-reload
systemctl enable weather-bot-monitor.timer

echo "Installed systemd units."
echo "Next safe checks:"
echo "  sudo -u ${SERVICE_USER} ${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py --status"
echo "  sudo -u ${SERVICE_USER} ${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py --live-readiness-check"
echo "Start monitor timer with: systemctl start weather-bot-monitor.timer"
echo "Start bot loop only after reviewing .env/config: systemctl start weather-bot"
