---
description: "Quick health check of server and agent via HTTP API — version, status, telemetry"
---

# /check-health

Diagnose current state of server and agent without SSH.

## Implementation

1. **Server health**: `curl -s --max-time 5 http://192.168.0.100:5059/api/health`
   - Check server_version, agent versions, connection status
2. **Server debug**: `curl -s --max-time 5 http://192.168.0.100:5059/api/debug`
   - Check SID maps, state keys, pending DB flags
3. **Agent status**: `curl -s --max-time 5 http://192.168.0.101:5059/api/agent/status`
   - Check api_token, control_mode, server_connected, node_id
4. **Fan telemetry**: `curl -s --max-time 5 http://192.168.0.100:5059/api/state | python3 -c "import sys,json; ..."`
   - Check fan health status, RPM, temperatures
5. **Telegram status**: `curl -s --max-time 5 http://192.168.0.100:5059/api/telegram/status`

## Report format
```
Server: v3.13.8 (git abc1234)
Agent:  v3.13.8 connected=true status=online
Fans:   3 healthy, 2 stopped
Agent fans: 1 stopped (DSM Fan)
Telegram: configured=true enabled=true
```
