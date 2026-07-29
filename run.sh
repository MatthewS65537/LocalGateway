#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

PORT="${1:-3456}"

# Kill any existing instance
pkill -f "localgateway.main" 2>/dev/null || true
sleep 1

echo "Starting LocalGateway on port $PORT..."
echo "Dashboard: http://127.0.0.1:$PORT"
echo "Logs: /tmp/lg-server.log"
echo "Stop: pkill -f localgateway.main"

nohup python3 -m localgateway.main > /tmp/lg-server.log 2>&1 &
echo $! > /tmp/lg-server.pid
echo "PID: $(cat /tmp/lg-server.pid)"