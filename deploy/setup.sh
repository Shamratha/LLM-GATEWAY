#!/usr/bin/env bash
#
# One-shot setup for the full stack on a fresh Ubuntu 22.04 VM
# (Oracle Cloud Always-Free ARM works well). Run from the repo root:
#
#   cp deploy/.env.example .env   # then edit .env
#   bash deploy/setup.sh
#
# It installs Docker, opens the VM firewall for HTTP/HTTPS, and brings the
# production stack up. Idempotent — safe to re-run.
set -euo pipefail

cd "$(dirname "$0")/.."   # repo root

if [ ! -f .env ]; then
  echo "ERROR: .env not found. Run:  cp deploy/.env.example .env  then edit it." >&2
  exit 1
fi

# 1. Docker + compose plugin -------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  echo ">> installing Docker..."
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
fi

# 2. VM firewall -------------------------------------------------------------
# Oracle's Ubuntu images ship a restrictive iptables INPUT chain; even with the
# Oracle security list open, 80/443 are dropped until we allow them here.
echo ">> opening ports 80/443 in the VM firewall..."
for port in 80 443; do
  if ! sudo iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
    sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport "$port" -j ACCEPT
  fi
done
if ! command -v netfilter-persistent >/dev/null 2>&1; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get update -y
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent
fi
sudo netfilter-persistent save

# 3. Bring up the stack ------------------------------------------------------
echo ">> building and starting the stack..."
sudo docker compose -f docker-compose.prod.yml --env-file .env up -d --build

DOMAIN=$(grep -E '^DOMAIN=' .env | cut -d= -f2-)
echo
echo "Done. Caddy is fetching a TLS cert (give it ~30-60s on first run)."
echo "  API:      https://${DOMAIN}/docs"
echo "  Grafana:  https://${DOMAIN}/grafana/"
echo
echo "Check logs with:  sudo docker compose -f docker-compose.prod.yml logs -f caddy"
