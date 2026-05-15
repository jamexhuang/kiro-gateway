#!/usr/bin/env bash
# Supervisor: graceful shutdown with timeout + auto-restart
# Usage: ./run.sh [--no-restart]

SHUTDOWN_TIMEOUT=5
AUTO_RESTART=true
SERVER_PID=""

if [[ "$1" == "--no-restart" ]]; then
  AUTO_RESTART=false
fi

export LATENCY_TRACING=true

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "[supervisor] Sending SIGTERM to server (PID $SERVER_PID)..."
    kill -TERM "$SERVER_PID" 2>/dev/null

    local waited=0
    while kill -0 "$SERVER_PID" 2>/dev/null && (( waited < SHUTDOWN_TIMEOUT )); do
      sleep 1
      (( waited++ ))
      echo "[supervisor] Waiting for graceful shutdown... ${waited}s/${SHUTDOWN_TIMEOUT}s"
    done

    if kill -0 "$SERVER_PID" 2>/dev/null; then
      echo "[supervisor] Timeout reached. Force killing PID $SERVER_PID."
      kill -9 "$SERVER_PID" 2>/dev/null
      wait "$SERVER_PID" 2>/dev/null
    else
      echo "[supervisor] Server stopped gracefully."
    fi
  fi
  SERVER_PID=""
}

trap 'AUTO_RESTART=false; cleanup; exit 0' INT TERM

start_server() {
  .venv/bin/python3 main.py &
  SERVER_PID=$!
  echo "[supervisor] Server started (PID $SERVER_PID) | LATENCY_TRACING=$LATENCY_TRACING"
}

cd "$(dirname "$0")"

while true; do
  start_server
  wait "$SERVER_PID"
  EXIT_CODE=$?
  echo "[supervisor] Server exited with code $EXIT_CODE"

  if [[ "$AUTO_RESTART" == "false" ]]; then
    echo "[supervisor] Auto-restart disabled. Exiting."
    exit $EXIT_CODE
  fi

  echo "[supervisor] Restarting in 2s..."
  sleep 2
done
