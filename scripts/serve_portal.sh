#!/usr/bin/env bash
# Starts the sample portal dev server on http://localhost:5173
set -euo pipefail
cd "$(dirname "$0")/../sample_portal"
if [[ ! -d node_modules ]]; then
  echo "Installing portal dependencies (first run) ..."
  npm install
fi
npm run dev
