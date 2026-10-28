#!/usr/bin/env bash
# c1_probe.sh — suíte LIVE do M0' contra o LXC 9102 real (rota P1, .0/24).
# Sobe serviço único + mock sink manualmente (sem root/systemd), dispara as
# sondas da proposta §6 e gera docs/reports/<data>-m0-prime-c1.md
#
# Uso (como qualquer user):  suite/c1_probe.sh
# Env opcionais: LISTEN_PORT (8480), UPSTREAM (192.168.0.200:4533), VHOST (vazio=P1)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$ROOT/docs/reports"
mkdir -p "$OUT_DIR"
REPORT="$OUT_DIR/$(date +%F)-m0-prime-c1.md"

LISTEN_PORT="${LISTEN_PORT:-8480}"
UPSTREAM="${UPSTREAM:-192.168.0.200:4533}"
VHOST="${VHOST:-}"
HOST_IP="$(hostname -i | awk '{print $1}')"

TMPD="$(mktemp -d /tmp/nvr-probe-XXXXXX)"
SOCK="$TMPD/mock-ipc.sock"
SPY="$TMPD/spy.ndjson"
SRV_LOG="$TMPD/srv.log"
PID_SRV=""
PID_SINK=""

cleanup() {
  [[ -n "$PID_SINK" ]] && kill "$PID_SINK" 2>/dev/null
  [[ -n "$PID_SRV" ]]  && kill "$PID_SRV" 2>/dev/null
  sleep 0.3
  cp "$SPY" "$OUT_DIR/spy-$(date +%H%M%S).ndjson" 2>/dev/null
  rm -rf "$TMPD"
}
trap cleanup EXIT

cd "$ROOT"

echo "[c1-probe] sobe mock sink -> $SOCK"
python3 tools/fake_mpv_sink.py --socket "$SOCK" >/dev/null 2>&1 &
PID_SINK=$!

echo "[c1-probe] sobe serviço único -> :$LISTEN_PORT (upstream $UPSTREAM${VHOST:+ vhost=$VHOST})"
env NVRA_LISTEN_ADDR=0.0.0.0 NVRA_LISTEN_PORT="$LISTEN_PORT" \
    NVRA_UPSTREAM="$UPSTREAM" NVRA_UPSTREAM_VHOST="${VHOST}" \
    NVRA_MPV_SOCKET="$SOCK" NVRA_SPY_FILE="$SPY" \
    python3 -m navidrome_remote >"$SRV_LOG" 2>&1 &
PID_SRV=$!

ok=""
for i in $(seq 1 40); do
  if curl -sf -o /dev/null "http://127.0.0.1:$LISTEN_PORT/api/v1/health"; then
    ok=1
    break
  fi
  sleep 0.25
done
if [[ -z "$ok" ]]; then
  echo "[c1-probe] FATAL: serviço não subiu; log:" >&2
  cat "$SRV_LOG" >&2
  exit 1
fi

BASE="http://127.0.0.1:$LISTEN_PORT"

{
  echo "# Relatório M0' — spike de protocolo em C1 (dev-workspace, .0/24)"
  echo
  echo "- **Quando:** $(date '+%F %T %Z')"
  VHOST_NOTE=""
  [[ -n "$VHOST" ]] && VHOST_NOTE=" (vhost \`$VHOST\`)"
  echo "- **Upstream:** \`$UPSTREAM\`$VHOST_NOTE"
  echo "- **Servidor escuta:** http://$HOST_IP:$LISTEN_PORT"
  echo
} > "$REPORT"

# ---------------------------------------------------------------- 1. básicos
{
  echo "## 1. Transparência básica (proxy)"
  echo
  echo "| probe | resultado |"
  echo "|---|---|"
  ROOT_OUT="$(curl -s -o /dev/null -w '%{http_code} -> %{redirect_url}' "$BASE/")"
  echo "| GET / (raiz) | \`$ROOT_OUT\` |"

  APP_HTML="$(curl -s "$BASE/app/")"
  CT_APP="$(curl -s -o /dev/null -w '%{content_type}' "$BASE/app/")"
  SNIPPET='<script src="/bridge.js"></script>'
  # count occurrences of the tag in the document (0 if upstream serves none)
  N_INJ="$(printf '%s' "$APP_HTML" | grep -oF "$SNIPPET" | wc -l | tr -d ' ')"
  if [[ "${N_INJ}" -ge 1 ]]; then
    echo "| GET /app/ | 200, \`$CT_APP\`, **injecao presente** (${N_INJ}x) — ${SNIPPET} |"
  else
    echo "| GET /app/ | **ATENCAO** \`$CT_APP\` SEM marca de injecao — investigar layout upstream |"
  fi

  CT_BRIDGE="$(curl -s -o /dev/null -w '%{content_type}' "$BASE/bridge.js")"
  SIZE_BRIDGE="$(curl -s "$BASE/bridge.js" | wc -c)"
  echo "| GET /bridge.js (asset local, sombra do upstream) | 200, \`$CT_BRIDGE\`, ${SIZE_BRIDGE} bytes |"
} >> "$REPORT"

# ------------------------------------------------------------ 2. OPTIONS (A)
{
  echo
  echo "## 2. Mapa OPTIONS/CORS por rota (Cláusula A) — via proxy, medido do .0/24"
  echo
  echo "| rota | status | ACAO | ACAM | ACAH | www-auth |"
  echo "|---|---|---|---|---|---|"
  # preflight real: o Navegador envia Origin — CORS do Navidrome depende disso
  ORIG="${PROBE_ORIGIN:-$BASE}"
  for route in "/" "/app/" "/rest/" "/rest/ping" "/stream/" "/cover-art/" "/playlists/" "/songs/" "/ws" "/api/v1/health"; do
    HDRS="$(curl -s -X OPTIONS -H "Origin: $ORIG" -D - -o /dev/null "$BASE$route")"
    ST="$(printf '%s' "$HDRS" | head -1 | awk '{print $2}')"
    grab() { printf '%s' "$HDRS" | grep -i "^$1:" | head -1 | sed "s/^[^:]*: //" | tr -d '\r'; }
    echo "| $route | $ST | $(grab access-control-allow-origin) | $(grab access-control-allow-methods) | $(grab access-control-allow-headers) | $(grab www-authenticate) |"
  done
  echo
  echo "> Leitura: linhas com \`www-auth\` mas sem \`ACAO\` = preflight 401 sem CORS (gateway não sintetiza — comportamento esperado p/ rotas REST protegidas, espelhado do upstream; ver KB media-stack)."
} >> "$REPORT"

# ----------------------------------------------------------- 3. WS (B i)
{
  echo
  echo "## 3. WebSocket (Cláusula B i) — candidatos sondados VIA PROXY"
  echo
  echo "| rota | status handshake | veredicto |"
  echo "|---|---|---|"
  python3 suite/ws_probe.py "$LISTEN_PORT" 2>/dev/null || echo "| (probe indisponível) | — | — |"
} >> "$REPORT"

# -------------------------------------------------------- 4. API + IPC (6)
{
  echo
  echo "## 4. /api/v1/* contra o sink falso (aceite 6)"
  echo
  echo "| # | chamada | http | corpo |"
  echo "|---|---|---|---|"
  # NOTE: o curl roda NO SHELL PAI (funcoes em $(...) perdiam a captura CAT;
  # metodo vem do par — POST sem corpo continua POST).
  i=0
  BF="$(mktemp /tmp/.nvr-cap.XXXX)"
  for pair in \
    "GET|$BASE/api/v1/health|" \
    "GET|$BASE/api/v1/status|" \
    "POST|$BASE/api/v1/play|{\"url\":\"http://192.168.0.200:4533/rest/stream?id=demo&u=x&s=y\"}" \
    "GET|$BASE/api/v1/status|" \
    "POST|$BASE/api/v1/pause|" \
    "GET|$BASE/api/v1/status|" \
    "POST|$BASE/api/v1/seek|{\"pos\":42}" \
    "POST|$BASE/api/v1/next|" \
    "POST|$BASE/api/v1/volume|{\"level\":30}" \
    "POST|$BASE/api/v1/stop|" \
    "GET|$BASE/api/v1/status|" ; do
    M="${pair%%|*}"; rest="${pair#*|}"; URL="${rest%%|*}"; BODY="${rest#*|}"
    i=$((i+1))
    if [[ "$M" == "GET" ]]; then
      CODE="$(curl -s -o "$BF" -w '%{http_code}' "$URL")"
    else
      CODE="$(curl -s -X "$M" -H 'content-type: application/json' -d "${BODY:-}" -o "$BF" -w '%{http_code}' "$URL")"
    fi
    CAT="$(cat "$BF" 2>/dev/null || true)"
    SHORT="${CAT:0:120}"
    [[ ${#CAT} -gt 120 ]] && SHORT="${SHORT}…"
    echo "| $i | $M $(basename "$URL") | $CODE | \`$SHORT\` |"
  done
  rm -f "$BF"
} >> "$REPORT"

# ---------------------------------------------------------- 5. spy digest
{
  echo
  echo "## 5. Espião NDJSON (aceite 5) — digest"
  echo
  TOTAL="$(wc -l < "$SPY" 2>/dev/null || echo 0)"
  echo "Total de eventos: **$TOTAL**"
  echo
  python3 "$HERE/_digest.py" "$SPY"
  echo
  REDS="$(grep -c '"event":"upgrade_red"' "$SPY" 2>/dev/null || true)"
  if [[ "${REDS:-0}" -ge 1 ]]; then
    echo "**⚠️ Cláusula B(i) — RED documentado:** $REDS evento(s) \`upgrade_red\` no spy (upstream recusa o handshake). Conforme aceite 1, RED COM EVIDÊNCIA é resultado válido; a decisão (fix / fallback) é do PO/Tech-lead."
  else
    echo "Nenhum \`upgrade_red\` registrado (ver §3 se houve candidato 101: verde B(i))."
  fi
  SCOUNT="$(grep -o '"set_cookie":\[[^]]*\]' "$SPY" 2>/dev/null | grep -vc '\[\]' || true)"
  echo
  echo "Eventos \`res\` com Set-Cookie names: **$SCOUNT** (valores NUNCA registrados — invariante de credenciais)."
} >> "$REPORT"

# ---------------------------------------------------------- 6. checklist
{
  echo
  echo "## 6. Verificação do aceite (referência cruzada)"
  echo
  CHECK_INJ="$(curl -s "$BASE/app/" | grep -c 'src="/bridge.js"' || true)"
  CHECK_HEALTH="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/v1/health")"
  printf -- '- [%s] **(4)** injeção presente no HTML do SPA real: %s ocorrência(s)\n' \
    "$([[ "${CHECK_INJ:-0}" -ge 1 ]] && echo x || echo )" "${CHECK_INJ:-0}"
  printf -- '- [%s] **(6)** /api/v1/health respondendo via serviço: http %s\n' \
    "$([[ "$CHECK_HEALTH" == "200" ]] && echo x || echo )" "$CHECK_HEALTH"
  printf -- '- [%s] **(5)** espião registrou o boot (≥3 eventos req): %s\n' \
    "$([[ "$TOTAL" -ge 3 ]] && echo x || echo )" "$TOTAL"
  echo
  echo "- [ ] (1) B(i) upgrade: interpretar §3 (verde=101 echo; red=recusa; ausente=player não usa WS — dado novo p/ spec)"
  echo "- [ ] (2) B(ii) Set-Cookie survive reload: requer SESSÃO real do Navidrome — executar em browser (QA) com a conta de serviço; mecânica de passthrough já coberta nos testes unitários"
  echo "- [ ] (3) B(iii)/A mapa CORS: tabela em §2 (interpretação no PR)"
  echo
  echo "_Gerado por \`suite/c1_probe.sh\` (C1, dev-workspace)._ Spy bruto copiado para \`docs/reports/spy-*.ndjson\` no cleanup._"
} >> "$REPORT"

echo "[c1-probe] PRONTO: $REPORT"
