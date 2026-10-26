#!/usr/bin/env bash
set -Eeuo pipefail

# -------------------------------------------------
# Resolve the real project directory.
# When auto-update is enabled, this script re-runs
# itself from /tmp before touching /lookup/start.sh.
# That prevents "unexpected end of file" errors
# caused by modifying the currently executing script.
# -------------------------------------------------
PROJECT_DIR="${LOOKUP_PROJECT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
AUTO_GIT_PULL="${AUTO_GIT_PULL:-false}"

if [ "$AUTO_GIT_PULL" = "true" ] && [ "${START_FROM_TEMP:-0}" != "1" ]; then
  TMP_START="$(mktemp /tmp/lookup-start.XXXXXX.sh)"
  cp "$0" "$TMP_START"
  chmod +x "$TMP_START"
  exec env \
    LOOKUP_PROJECT_DIR="$PROJECT_DIR" \
    START_FROM_TEMP=1 \
    START_TEMP_FILE="$TMP_START" \
    AUTO_GIT_PULL="$AUTO_GIT_PULL" \
    bash "$TMP_START"
fi

if [ "${START_FROM_TEMP:-0}" = "1" ] && [ -n "${START_TEMP_FILE:-}" ]; then
  trap 'rm -f "$START_TEMP_FILE"' EXIT
fi

cd "$PROJECT_DIR"

# -------------------------------------------------
# Config
# -------------------------------------------------
API_PORT="${API_PORT:-8000}"
# Lookup-only settings; .env values are loaded after Python dependencies install.
PUBLIC_API_URL="https://api.teamsecret.in"
CF_DIR="${LOOKUP_CF_DIR:-/root/lookup-cf-home/.cloudflared}"
CF_CONFIG="${CF_DIR}/lookup-api-new.yml"  # Isolated from DB2/default config.yml
BOT_SCREEN="lookup-bot"
CF_SCREEN="lookup-tunnel-new"  # Never stop DB2's session

# -------------------------------------------------
# Safe Git update
# -------------------------------------------------
if [ "$AUTO_GIT_PULL" = "true" ] \
  && command -v git >/dev/null 2>&1 \
  && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then

  echo "🔄 Checking GitHub for updates..."

  CURRENT_BRANCH="$(git branch --show-current 2>/dev/null || true)"
  [ -n "$CURRENT_BRANCH" ] || CURRENT_BRANCH="main"

  OTHER_CHANGES=0
  START_CHANGED=0

  while IFS= read -r file; do
    [ -n "$file" ] || continue
    if [ "$file" = "start.sh" ]; then
      START_CHANGED=1
    else
      OTHER_CHANGES=1
    fi
  done < <(git diff --name-only)

  if [ "$OTHER_CHANGES" -eq 1 ]; then
    echo "⚠️ Other tracked local changes found; Git update skipped."
    echo "   Check with: git status"
  else
    START_BACKUP=""

    if [ "$START_CHANGED" -eq 1 ]; then
      START_BACKUP="$(mktemp /tmp/start-backup.XXXXXX.sh)"
      cp "$PROJECT_DIR/start.sh" "$START_BACKUP"
      git restore --source=HEAD -- start.sh
    fi

    if git pull --ff-only origin "$CURRENT_BRANCH"; then
      echo "✅ Repository updated."
    else
      echo "⚠️ Git pull failed; continuing with current code."
    fi

    if [ -n "$START_BACKUP" ] && [ -f "$START_BACKUP" ]; then
      cp "$START_BACKUP" "$PROJECT_DIR/start.sh"
      chmod +x "$PROJECT_DIR/start.sh"
      rm -f "$START_BACKUP"
      echo "✅ Local customized start.sh restored safely."
    fi
  fi
fi

# -------------------------------------------------
# Basic checks
# -------------------------------------------------
if [ ! -f ".env" ]; then
  echo "❌ .env missing in $PROJECT_DIR"
  exit 1
fi

# -------------------------------------------------
# Local encryption key for sensitive DB settings
# -------------------------------------------------
if ! grep -Eq '^APP_ENCRYPTION_KEY=.+$' .env; then
  echo "🔐 Generating APP_ENCRYPTION_KEY for encrypted bot settings..."
  ENC_KEY="$(python3 - <<'PY_KEY'
import base64, secrets
print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
PY_KEY
)"
  printf '\nAPP_ENCRYPTION_KEY=%s\n' "$ENC_KEY" >> .env
  chmod 600 .env 2>/dev/null || true
  echo "✅ Encryption key saved in .env."
  echo "⚠️  Copy .env securely when migrating servers; do not commit it to Git."
fi

if ! command -v screen >/dev/null 2>&1; then
  echo "📦 Installing screen..."
  apt-get update
  apt-get install -y screen
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "📦 Installing curl..."
  apt-get update
  apt-get install -y curl
fi

# -------------------------------------------------
# Python dependencies
# -------------------------------------------------
echo "📦 Installing Python dependencies..."
if python3 -m pip install -r requirements.txt; then
  echo "✅ Python dependencies installed."
else
  echo "⚠️ Normal pip install failed; retrying with --break-system-packages..."
  python3 -m pip install --break-system-packages -r requirements.txt
  echo "✅ Python dependencies installed with --break-system-packages."
fi

# -------------------------------------------------
# Install cloudflared if missing
# -------------------------------------------------
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "☁️ Installing cloudflared..."
  mkdir -p /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
    | tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
  echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
    > /etc/apt/sources.list.d/cloudflared.list
  apt-get update
  apt-get install -y cloudflared
fi

# -------------------------------------------------
# Named-tunnel credentials are required after migrating servers.
# A UUID by itself cannot connect. DNS is ALREADY mapped to this tunnel
# and does not have to be recreated on every new VPS.
# -------------------------------------------------
mapfile -d '' -t CF_VALUES < <(python3 - <<'PY_ENV'
from dotenv import dotenv_values
import os, sys
cfg = dotenv_values('.env')
for name, default in (
    ('CLOUDFLARE_TUNNEL_ID', '2f9a8433-c321-407b-8a42-f15ec0ae2057'),
    ('PUBLIC_API_URL', 'https://api.teamsecret.in'),
):
    sys.stdout.write(str(os.environ.get(name) or cfg.get(name) or default) + '\0')
PY_ENV
)
TUNNEL_ID="${CF_VALUES[0]}"
PUBLIC_API_URL="${CF_VALUES[1]%/}"
CF_CREDENTIALS="${LOOKUP_CF_CREDENTIALS:-${CF_DIR}/${TUNNEL_ID}.json}"
mkdir -p "$CF_DIR"
chmod 700 "$CF_DIR"

if [ ! -r "$CF_CREDENTIALS" ]; then
  echo "❌ Missing credentials for lookup-api-new: $CF_CREDENTIALS"
  echo "   Securely copy the matching JSON credential from your current server:"
  echo "   /root/lookup-cf-home/.cloudflared/${TUNNEL_ID}.json"
  echo "   The Tunnel ID alone is insufficient; no changes were made to DB2/DNS."
  exit 1
fi
chmod 600 "$CF_CREDENTIALS" 2>/dev/null || true
cat > "$CF_CONFIG" <<EOF_CF
tunnel: ${TUNNEL_ID}
credentials-file: ${CF_CREDENTIALS}

ingress:
  - hostname: api.teamsecret.in
    service: http://127.0.0.1:${API_PORT}
  - service: http_status:404
EOF_CF
if ! cloudflared --config "$CF_CONFIG" tunnel ingress validate >/dev/null 2>&1; then
  echo "❌ Invalid lookup ingress config: $CF_CONFIG"
  exit 1
fi
echo "✅ Lookup-only Cloudflare config generated: $CF_CONFIG"

# -------------------------------------------------
# Stop old sessions
# -------------------------------------------------
# Do not kill the currently working API process or unrelated Cloudflare sessions.
# If Lookup already has its own named screen, restart only that screen.
screen -S "$CF_SCREEN" -X quit 2>/dev/null || true

# -------------------------------------------------
# Start bot + local FastAPI
# Force API_PORT=8000 and disable the bot's internal
# token-based tunnel manager. The named tunnel below
# is the only Cloudflare tunnel used.
# -------------------------------------------------
echo "🤖 Starting bot/API on port ${API_PORT}..."

if curl -fsS "http://127.0.0.1:${API_PORT}/" >/dev/null 2>&1; then
  echo "✅ API already listening on port $API_PORT; not starting another copy."
else
  screen -S "$BOT_SCREEN" -X quit 2>/dev/null || true
  screen -dmS "$BOT_SCREEN" bash -lc \
    "cd '$PROJECT_DIR' && export API_PORT='$API_PORT' AUTO_CLOUDFLARE_TUNNEL=false && python3 bot.py"
fi

API_OK=0
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${API_PORT}/" >/dev/null 2>&1; then
    API_OK=1
    echo "✅ Local API is online on port ${API_PORT}."
    break
  fi

  if ! screen -ls 2>/dev/null | grep -q "\.${BOT_SCREEN}[[:space:]]"; then
    echo "❌ Bot/API process exited before becoming ready."
    echo "Run manually to inspect the error:"
    echo "   cd $PROJECT_DIR && API_PORT=$API_PORT AUTO_CLOUDFLARE_TUNNEL=false python3 bot.py"
    exit 1
  fi

  sleep 1
done

if [ "$API_OK" -ne 1 ]; then
  echo "❌ Local API did not become ready within 60 seconds."
  echo "Logs: screen -r $BOT_SCREEN"
  exit 1
fi

# -------------------------------------------------
# Start named Cloudflare tunnel
# -------------------------------------------------
echo "☁️ Starting Cloudflare tunnel..."

screen -dmS "$CF_SCREEN" cloudflared --config "$CF_CONFIG" tunnel run "$TUNNEL_ID"

sleep 5

if ! screen -ls 2>/dev/null | grep -q "\.${CF_SCREEN}[[:space:]]"; then
  echo "❌ Cloudflare tunnel exited."
  echo "Run manually:"
  echo "   cloudflared --config $CF_CONFIG tunnel run $TUNNEL_ID"
  exit 1
fi

# -------------------------------------------------
# Public health check
# -------------------------------------------------
PUBLIC_OK=0
for i in $(seq 1 30); do
  if curl -fsS "${PUBLIC_API_URL}/" 2>/dev/null | grep -q '"service":"lookup-api"'; then
    PUBLIC_OK=1
    break
  fi
  sleep 1
done

echo
if [ "$PUBLIC_OK" -eq 1 ]; then
  echo "✅ Everything started successfully."
  echo "🌐 API: $PUBLIC_API_URL"
else
  echo "⚠️ Public API is not returning the expected lookup-api health response."
  echo "   Check DNS CNAME -> ${TUNNEL_ID}.cfargotunnel.com"
  echo "Tunnel logs: screen -r $CF_SCREEN"
fi

echo "🤖 Bot logs (if started here): screen -r $BOT_SCREEN"
echo "☁️ Tunnel logs: screen -r $CF_SCREEN"
echo "📋 Sessions:    screen -ls"
