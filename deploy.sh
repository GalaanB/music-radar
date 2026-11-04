#!/usr/bin/env bash
# Met le serveur à jour depuis GitHub puis relance le bot.
# Usage : bash ~/music-radar/deploy.sh
set -euo pipefail
cd "$(dirname "$0")"
git pull --ff-only
docker compose up -d --build
docker compose ps
