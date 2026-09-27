#!/bin/bash
# =============================================================================
#  clipbot — installation automatique sur Oracle Cloud « Always Free »
#
#  À coller dans : Create instance → Show advanced options → Management →
#  « Paste cloud-init script ». Remplis seulement les 5 lignes ci-dessous.
#  Aucune commande à taper : la machine installe tout et démarre le bot seule,
#  puis vérifie GitHub toutes les 10 min et se met à jour d'elle-même.
# =============================================================================
REPO="pseudo/clipbot"             # ton dépôt GitHub
GITHUB_TOKEN=""                   # vide si le dépôt est public ; sinon jeton GitHub en lecture seule
TELEGRAM_BOT_TOKEN=""             # donné par @BotFather
TELEGRAM_PAIR_CODE=""             # ton code inventé (pour /start)
ALLOWED_CHANNELS=""               # ex : kamet0,zerator  (vide = tendances, clés Twitch requises)
GROQ_API_KEY=""                   # optionnel : transcription cloud gratuite
# =============================================================================

set -eu
exec > /var/log/clipbot-install.log 2>&1
APP=/opt/clipbot
mkdir -p "$APP/data"

# 1. Paquets + 2 Go de swap (marge de sécurité pour les montages)
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y docker.io git
systemctl enable --now docker
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# 2. Code source
if [ -n "$GITHUB_TOKEN" ]; then
  URL="https://x-access-token:${GITHUB_TOKEN}@github.com/${REPO}.git"
else
  URL="https://github.com/${REPO}.git"
fi
[ -d "$APP/src/.git" ] || git clone --depth 1 "$URL" "$APP/src"

# 3. Configuration (lisible uniquement par root)
cat > "$APP/.env" <<ENV
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_PAIR_CODE=${TELEGRAM_PAIR_CODE}
ALLOWED_CHANNELS=${ALLOWED_CHANNELS}
GROQ_API_KEY=${GROQ_API_KEY}
CLIPBOT_WORKDIR=/data
MAX_CONCURRENT_STREAMS=3
WHISPER_MODEL=small
WHISPER_THREADS=2
WHISPER_KEEP_LOADED=true
LANGUAGES=fr
ENV
chmod 600 "$APP/.env"

# 4. Script de (re)déploiement : reconstruit seulement si GitHub a changé
cat > "$APP/deploy.sh" <<'DEPLOY'
#!/bin/bash
set -eu
cd /opt/clipbot/src
git fetch -q --depth 1 origin main
NEW=$(git rev-parse origin/main)
OLD=$(cat /opt/clipbot/.deployed 2>/dev/null || true)
RUNNING=$(docker ps -q -f name=^clipbot$)
if [ "$NEW" = "$OLD" ] && [ -n "$RUNNING" ] && [ "${FORCE:-0}" != "1" ]; then exit 0; fi
git reset -q --hard origin/main
docker build ${FORCE:+--pull --no-cache} -t clipbot .   # FORCE=1 : yt-dlp à jour
docker rm -f clipbot >/dev/null 2>&1 || true
docker run -d --name clipbot --restart always \
  --env-file /opt/clipbot/.env -v /opt/clipbot/data:/data \
  -p 127.0.0.1:8080:8080 --log-opt max-size=20m --log-opt max-file=3 clipbot
echo "$NEW" > /opt/clipbot/.deployed
docker image prune -f >/dev/null
echo "$(date -Is) déployé $NEW"
DEPLOY
chmod 700 "$APP/deploy.sh"

# 5. Mises à jour automatiques : toutes les 10 min + reconstruction complète le lundi
cat > /etc/cron.d/clipbot <<'CRON'
*/10 * * * * root /opt/clipbot/deploy.sh >> /var/log/clipbot-deploy.log 2>&1
30 4 * * 1 root FORCE=1 /opt/clipbot/deploy.sh >> /var/log/clipbot-deploy.log 2>&1
CRON

# 6. Premier démarrage
"$APP/deploy.sh"
echo "clipbot installé."
