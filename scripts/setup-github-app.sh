#!/usr/bin/env bash
# Creates a GitHub App for claude-agent using the GitHub App Manifest flow.
#
# Designed to be driven by the /setup-github-app Claude skill, which handles
# all user interaction. Each subcommand does one step and exits.
#
# Requirements:
#   - gh CLI (https://cli.github.com) — authenticated with `gh auth login`
#   - python3 (for the local callback server)
#
# Subcommands:
#   check-prereqs                     Check gh and python3 are available
#   start-server <repo> <app-name>    Start the callback server + open browser
#   wait-for-callback                 Wait for GitHub to redirect back with a code
#   exchange-code                     Exchange the code for app credentials (outputs JSON)
#   open-install <app-slug>           Open the app installation page in the browser
#   get-installation-id <app-id>      Fetch the installation ID via gh API
#   format-env <json-creds> <inst-id> Format the credentials as .env lines

set -euo pipefail

CALLBACK_PORT=33333
CODE_FILE="/tmp/github-app-code"
PID_FILE="/tmp/github-app-server.pid"

cmd="${1:-}"
shift || true

case "$cmd" in

# ── check-prereqs ──────────────────────────────────────────────────────────────
check-prereqs)
    errors=""
    if ! command -v gh &>/dev/null; then
        errors="${errors}gh CLI is not installed. Install: brew install gh\n"
    elif ! gh auth status &>/dev/null 2>&1; then
        errors="${errors}gh is not authenticated. Run: gh auth login\n"
    fi
    if ! command -v python3 &>/dev/null; then
        errors="${errors}python3 is not installed.\n"
    fi
    if [ -n "$errors" ]; then
        echo -e "$errors"
        exit 1
    fi
    echo "ok"
    ;;

# ── start-server ──────────────────────────────────────────────────────────────
start-server)
    REPO="${1:?Usage: start-server <owner/repo> <app-name>}"
    APP_NAME="${2:?Usage: start-server <owner/repo> <app-name>}"

    MANIFEST=$(cat <<EOF
{
  "name": "$APP_NAME",
  "url": "https://github.com/$REPO",
  "hook_attributes": {"url": "https://example.com/webhook/github", "active": false},
  "redirect_url": "http://localhost:$CALLBACK_PORT/callback",
  "public": false,
  "default_permissions": {
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
    "metadata": "read",
    "workflows": "write"
  },
  "default_events": ["issues", "issue_comment", "pull_request", "push"]
}
EOF
    )

    MANIFEST_FOR_PY=$(echo "$MANIFEST" | python3 -c "import sys; print(repr(sys.stdin.read()))")

    CALLBACK_SCRIPT=$(mktemp /tmp/github-app-callback-XXXXXX.py)

    cat > "$CALLBACK_SCRIPT" <<PYEOF
import http.server, urllib.parse, os, html, threading

PORT = $CALLBACK_PORT
CODE_FILE = "$CODE_FILE"
MANIFEST = $MANIFEST_FOR_PY

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path in ("/", ""):
            manifest_escaped = html.escape(MANIFEST)
            body = f"""<!DOCTYPE html>
<html>
<head><title>Creating GitHub App...</title></head>
<body>
  <p>Redirecting to GitHub to create your app...</p>
  <form id="f" method="post" action="https://github.com/settings/apps/new">
    <input type="hidden" name="manifest" value="{manifest_escaped}">
  </form>
  <script>document.getElementById('f').submit();</script>
</body>
</html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        elif parsed.path == "/callback":
            code = params.get("code", [None])[0]
            if code:
                with open(CODE_FILE, "w") as f:
                    f.write(code)
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h2>App created! You can close this tab.</h2></body></html>")
                threading.Timer(0.5, self.server.shutdown).start()
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing code parameter")
        else:
            self.send_response(404)
            self.end_headers()

server = http.server.HTTPServer(("localhost", PORT), Handler)
server.serve_forever()
PYEOF

    rm -f "$CODE_FILE"

    python3 "$CALLBACK_SCRIPT" &
    SERVER_PID=$!
    echo "$SERVER_PID" > "$PID_FILE"
    sleep 0.5

    # Open browser
    LAUNCH_URL="http://localhost:$CALLBACK_PORT"
    if command -v open &>/dev/null; then
        open "$LAUNCH_URL"
    elif command -v xdg-open &>/dev/null; then
        xdg-open "$LAUNCH_URL"
    fi

    echo "server_pid=$SERVER_PID"
    echo "url=$LAUNCH_URL"
    ;;

# ── wait-for-callback ──────────────────────────────────────────────────────────
wait-for-callback)
    WAITED=0
    while [ ! -f "$CODE_FILE" ]; do
        sleep 1
        WAITED=$((WAITED + 1))
        if [ $WAITED -ge 180 ]; then
            # Clean up server
            [ -f "$PID_FILE" ] && kill "$(cat "$PID_FILE")" 2>/dev/null && rm -f "$PID_FILE"
            echo "TIMEOUT"
            exit 1
        fi
    done

    CODE=$(cat "$CODE_FILE")
    rm -f "$CODE_FILE"

    # Clean up server
    [ -f "$PID_FILE" ] && kill "$(cat "$PID_FILE")" 2>/dev/null && rm -f "$PID_FILE"

    echo "$CODE"
    ;;

# ── exchange-code ──────────────────────────────────────────────────────────────
exchange-code)
    CODE="${1:?Usage: exchange-code <code>}"

    # Returns full JSON with id, slug, pem, webhook_secret
    gh api \
        --method POST \
        "/app-manifests/$CODE/conversions" \
        --jq '{id: .id, pem: .pem, webhook_secret: .webhook_secret, name: .name, slug: .slug}'
    ;;

# ── open-install ───────────────────────────────────────────────────────────────
open-install)
    APP_SLUG="${1:?Usage: open-install <app-slug>}"
    INSTALL_URL="https://github.com/apps/$APP_SLUG/installations/new"

    if command -v open &>/dev/null; then
        open "$INSTALL_URL"
    elif command -v xdg-open &>/dev/null; then
        xdg-open "$INSTALL_URL"
    fi

    echo "$INSTALL_URL"
    ;;

# ── get-installation-id ───────────────────────────────────────────────────────
get-installation-id)
    APP_ID="${1:?Usage: get-installation-id <app-id> <pem>}"
    APP_PEM="${2:?Usage: get-installation-id <app-id> <pem>}"

    # Build a JWT using openssl (no pip dependencies needed on the local machine).
    # PyJWT is a runtime dependency inside the Docker container, not a setup dependency.
    NOW=$(date +%s)
    HEADER=$(printf '{"alg":"RS256","typ":"JWT"}' | openssl base64 -e | tr -d '=\n' | tr '/+' '_-')
    PAYLOAD=$(printf '{"iat":%d,"exp":%d,"iss":%s}' "$((NOW - 60))" "$((NOW + 600))" "$APP_ID" \
        | openssl base64 -e | tr -d '=\n' | tr '/+' '_-')
    SIGNATURE=$(printf '%s.%s' "$HEADER" "$PAYLOAD" \
        | openssl dgst -sha256 -sign <(echo "$APP_PEM") \
        | openssl base64 -e | tr -d '=\n' | tr '/+' '_-')
    APP_JWT="${HEADER}.${PAYLOAD}.${SIGNATURE}"

    INSTALLATION_ID=$(curl -sf \
        -H "Authorization: Bearer $APP_JWT" \
        -H "Accept: application/vnd.github+json" \
        "https://api.github.com/app/installations" \
        | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0]['id'] if d else 'NOT_FOUND')" \
        2>/dev/null || echo "NOT_FOUND")

    if [ "$INSTALLATION_ID" = "NOT_FOUND" ] || [ -z "$INSTALLATION_ID" ]; then
        echo "NOT_FOUND"
        exit 1
    fi
    echo "$INSTALLATION_ID"
    ;;

# ── format-env ─────────────────────────────────────────────────────────────────
format-env)
    CREDS_JSON="${1:?Usage: format-env <json-creds> <installation-id>}"
    INSTALLATION_ID="${2:?Usage: format-env <json-creds> <installation-id>}"

    python3 -c "
import json, sys
creds = json.loads(sys.argv[1])
inst_id = sys.argv[2]
app_id = creds['id']
slug = creds['slug']
pem_oneline = creds['pem'].replace(chr(10), r'\n')
bot_email = f\"{app_id}+{slug}[bot]@users.noreply.github.com\"

print(f'GITHUB_APP_ID={app_id}')
print(f'GITHUB_APP_PRIVATE_KEY=\"{pem_oneline}\"')
print(f'GITHUB_APP_INSTALLATION_ID={inst_id}')
print(f'GITHUB_BOT_NAME={slug}[bot]')
print(f'GITHUB_BOT_EMAIL={bot_email}')
print(f'GITHUB_WEBHOOK_SECRET={creds[\"webhook_secret\"]}')
" "$CREDS_JSON" "$INSTALLATION_ID"
    ;;

# ── cleanup ────────────────────────────────────────────────────────────────────
cleanup)
    [ -f "$PID_FILE" ] && kill "$(cat "$PID_FILE")" 2>/dev/null; rm -f "$PID_FILE"
    rm -f "$CODE_FILE"
    rm -f /tmp/github-app-callback-*.py
    echo "ok"
    ;;

*)
    echo "Unknown command: $cmd"
    echo "Commands: check-prereqs, start-server, wait-for-callback, exchange-code, open-install, get-installation-id, format-env, cleanup"
    exit 1
    ;;
esac
