#!/bin/bash
# Quick update: bump version → push → restart server → trigger agents
# Usage: ./update.sh [version]
# Example: ./update.sh 3.12.87

set -e

VERSION="${1:-}"
SERVER="192.168.0.100:5059"

if [ -z "$VERSION" ]; then
    # Auto-bump patch version
    CURRENT=$(grep 'CONFIG_VERSION' core/state.py | head -1 | grep -oP '"[0-9.]+"' | tr -d '"')
    MAJOR=$(echo $CURRENT | cut -d. -f1)
    MINOR=$(echo $CURRENT | cut -d. -f2)
    PATCH=$(echo $CURRENT | cut -d. -f3)
    VERSION="${MAJOR}.${MINOR}.$((PATCH + 1))"
    echo "Auto-bumped: $CURRENT → $VERSION"
fi

# 1. Bump version
sed -i "s/CONFIG_VERSION = \".*\"/CONFIG_VERSION = \"$VERSION\"/" core/state.py
echo "✓ Version set to $VERSION"

# 2. Commit + push
git add core/state.py
git commit -m "v$VERSION" --quiet
git push origin main --quiet
echo "✓ Pushed to GitHub"

# 3. Wait for git propagation
sleep 3

# 4. Update server
echo -n "Restarting server... "
RESP=$(curl -s -X POST "http://$SERVER/api/update/apply" -H 'Content-Type: application/json')
if echo "$RESP" | grep -q '"ok"'; then
    echo "OK"
else
    echo "FAILED: $RESP"
    exit 1
fi

# 5. Wait for server to come back
echo -n "Waiting for server... "
for i in $(seq 1 20); do
    sleep 2
    HTTP=$(curl -s -o /dev/null -w "%{http_code}" "http://$SERVER/" 2>/dev/null)
    if [ "$HTTP" = "200" ]; then
        echo "ready (${i}*2s)"
        break
    fi
    if [ $i -eq 20 ]; then
        echo "TIMEOUT"
        exit 1
    fi
done

# 6. Trigger agent update
echo -n "Triggering agents... "
RESP=$(curl -s -X POST "http://$SERVER/api/update/agents" -H 'Content-Type: application/json' -d '{}')
echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('message',''))" 2>/dev/null || echo "$RESP"

# 7. Show status
echo ""
echo "=== Status ==="
curl -s "http://$SERVER/api/health" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(f'Server: {d[\"server_version\"]}')
for a in d['agents']:
    p = ' ⚠PENDING' if a['pending'] else ''
    print(f'  {a[\"node_id\"]}: {a[\"version\"]} [{a[\"status\"]}]{p}')
"
