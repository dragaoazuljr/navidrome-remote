#!/usr/bin/env bash
# install.sh — instala o serviço único + unidade do motor (perfis c1|sbc).
# Uso:  sudo deploy/install.sh c1      # dev-workspace: serviço + MOCK sink
#       sudo deploy/install.sh sbc     # Orange Pi Zero 2W: serviço + mpv real
#
# Idempotente: re-execução atualiza código/units; o EnvironmentFile é tocado
# APENAS se inexistente (credenciais nunca são sobrescritas por oops).
set -euo pipefail

PROFILE="${1:?uso: install.sh c1|sbc}"
REPO_SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/navidrome-remote
ENV_FILE=/etc/navidrome-remote/env

[[ "$EUID" == 0 ]] || { echo "rodar como root"; exit 1; }
[[ "$PROFILE" == c1 || "$PROFILE" == sbc ]] || { echo "perfil inválido: $PROFILE"; exit 1; }

echo "== criando usuário navidrome-remote"
id -u navidrome-remote >/dev/null 2>&1 || useradd --system --home "$DEST" --shell /usr/sbin/nologin navidrome-remote

echo "== copiando código -> $DEST"
mkdir -p "$DEST"
cp -r "$REPO_SRC/navidrome_remote" "$DEST/"
cp -r "$REPO_SRC/tools" "$DEST/"

echo "== EnvironmentFile"
mkdir -p "$(dirname "$ENV_FILE")"
if [[ ! -f "$ENV_FILE" ]]; then
  if [[ "$PROFILE" == c1 ]]; then
    sed "s|^NVRA_MPV_SOCKET=.*|NVRA_MPV_SOCKET=/dev/shm/navidrome-remote-mock-ipc|" \
      "$REPO_SRC/deploy/env.example" > "$ENV_FILE"
  else
    cp "$REPO_SRC/deploy/env.example" "$ENV_FILE"
  fi
  chmod 640 "$ENV_FILE"
  chown navidrome-remote "$ENV_FILE"
  echo "   $ENV_FILE criado (NO TOUCH em execucoes seguintes — preencher NAV_USER/NAV_PASS a mao)"
else
  echo "   $ENV_FILE ja existe — mantido intacto"
fi
chmod 750 "$DEST"
chown -R navidrome-remote "$DEST"

echo "== unidades systemd"
cp "$REPO_SRC/deploy/navidrome-remote.service" /etc/systemd/system/
if [[ "$PROFILE" == c1 ]]; then
  cp "$REPO_SRC/deploy/fake-mpv-sink.service" /etc/systemd/system/
  systemctl enable fake-mpv-sink
  systemctl restart fake-mpv-sink
else
  cp "$REPO_SRC/deploy/navidrome-remote-mpv.service" /etc/systemd/system/
  systemctl enable navidrome-remote-mpv
fi
systemctl daemon-reload
systemctl enable navidrome-remote
sleep 1
systemctl restart navidrome-remote

echo "== estado"
systemctl --no-pager --full status navidrome-remote | head -12
echo
PORT="$(grep -E '^NVRA_LISTEN_PORT=' "$ENV_FILE" | cut -d= -f2 || true)"
PORT="${PORT:-80}"
echo "== fumaca: /bridge.js via servico"
sleep 1
curl -sS -o /dev/null -w "GET /bridge.js -> %{http_code}\n" "http://127.0.0.1:${PORT}/bridge.js" || true

echo
echo "INSTALL CONCLUIDO (perfil $PROFILE)."
echo "Proximo passo: suite C1 (suite/c1_probe.sh) antes de publicar."
