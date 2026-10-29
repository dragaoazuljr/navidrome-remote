# navidrome-remote

Servico unico e leve que roda num SoC (hoje dev-workspace x86; alvo Orange Pi Zero 2W 1 GB)
e transforma a instancia oficial **Navidrome v0.64.2** (LXC 9102) em um app mobile:
ele **intercepta e enriquece o que ja funciona** — proxy reverso transparente,
injecao do `bridge.js`, controle do playback via `mpv` (JSON-IPC) e espiao NDJSON.
Zero fork do Navidrome; zero dependencia compilada (CPython 3 stdlib puro / asyncio).

Contrato: [`proposals/2026-10-27-timeline-devws-pi-2w.md`](proposals/) (ratificado 2026-10-27; spec v1.2 + adendo D1)
e KB `dot-config/kb/scopes/personal/projects/navidrome-remote.md`.

## Servico unico (o que esta aqui)

| modulo | papel |
|---|---|
| `navidrome_remote/proxy.py` | o coracao: roteia, injeta, passa bytes. Termina HTTP SOMENTE para as rotas proprias (`/bridge.js`, `/api/v1/*`), HTML transformado e handshakes 101. O resto transita intacto (Clausula B; hop-by-hop removidos conforme RFC 7230). |
| `navidrome_remote/http1.py` | HTTP/1.1 minimalista (parser de headers, codecs chunked/gzip/zlib, StreamReader com splice) — dogfoodado tambem pelos testes. |
| `navidrome_remote/inject.py` | injeca `<script src="/bridge.js">` antes de `</head>` (fallbacks `<body>`/`<html>`), idempotente, case-insensitive. |
| `navidrome_remote/api.py` | `/api/v1/*` completo (A3): health (publico), status, play, pause, resume, next, prev, stop, seek, shuffle, repeat, volume, queue (GET/POST), spy (beacon). Toda rota (menos /health) passa pelo gate de sessao. `play` com `id` monta a URL Subsonic no lado servidor (NUNCA no navegador — invariante). |
| `navidrome_remote/gateway.py` | gate de sessao em 3 camadas (A3): L1 presenca (jwt > u/t/s/p > cookie opaco), L2 anti-conta-de-servico (JWT claim vs `NVRA_NAV_USER`, antes de qualquer probe), L3 liveness POR CHAMADA (probe barato `GET /rest/getIndexes?letter=A` contra o upstream; dedupe 10s de rate, fail-closed). Respostas levam so codigos de maquina. |
| `navidrome_remote/controller.py` | traducao 1 comando por acao para o mpv JSON-IPC (A4): play=loadfile replace; pause/resume=set_property; stop; next/prev=playlist-next/prev; seek absoluto; shuffle 0/1; repeat=loop-file/loop-playlist (conforme o modo); volume; reconciliacao engine->store. |
| `navidrome_remote/queue.py` | store de fila/settings (A3+A5): itens por id (URLs NUNCA trafegam), settings espelhadas, persistencia atomica (write-tmp+rename) crash-safe para a sdcard do 2W. |
| `navidrome_remote/ipc.py` | cliente mpv: socket AF_UNIX, JSON-NL, correlacao por `request_id`, eventos soltos para hook. |
| `navidrome_remote/spy.py` | espiao NDJSON append+fsync: toda requisicao/resposta/cookie(nome)/upgrade/erro. Nomes de cookies SIM, valores NUNCA. Desde A2.3 redige no chokepoint de escrita (nomes e FORMAS de segredos), qualquer que seja o chamador. |
| `navidrome_remote/assets/bridge.js` | o app no browser (A6, final): (1) **swap silencioso** — toda URL `/rest/stream` no `<audio>` vira um feed silencio gerado em memoria (zero consumo de upstream) e o id vai para `/play`; (2) **rotador de intencoes** — play/pause/seek/volume/next/prev/stop/shuffle/repeat (deteccao de controles oficiais do player) dispara `/api/v1/*` com a SESSAO da pagina (Authorization header; o gate L3 anda sobre ela); (3) **sincronia reversa** — poll leve de `/status` projeta estado no player nativo (inclusive re-disparo de troca de faixa quando o motor avanca sozinho). Estados: passive (pre-login, invisivel), engaged, degradado (anchor-drift / gate 401/403 / fatal — com restauracao total do modo nativo + badge explicativo). Metadados: anchor v0.64.2. |
| `navidrome_remote/config.py` | 100% EnvironmentFile (`NVRA_*`); falha rapido (stderr + exit!=0) se mal configurado. |
| `navidrome_remote/__main__.py` | entrypoint `python3 -m navidrome_remote` (banner classifica a rota P1/P2/custom). |
| `tools/fake_mpv_sink.py` | MOCK do mpv para C1 (mesmo contrato IPC; playlist-aware, auto-avance EOF, shuffle/repeat, `--null-audio` equivalencia) — permite exercitar todo o playback sem hardware de audio. |

Estado atual: M1 completo — session-gate (A3), controlador do motor (A4),
persistencia crash-safe (A5) e o `bridge.js` final com intercepcao dos
eventos nativos (A6). O gap restante ate o M3 e operacionais (deploy no
2W real, verificacao contra a instancia viva do LXC).

## Rotas do upstream (adendo D1 — uma linha no EnvironmentFile)

| rota | `NVRA_UPSTREAM` | `NVRA_UPSTREAM_VHOST` | quando |
|---|---|---|---|
| **P1** LAN crua | `192.168.0.200:4533` | (vazio) | .0/24 — padrao do env.example |
| **P2** tailnet | `100.90.124.50:80` | `navidrome.lan` | sem gateway novo; ts-gw ja existe |

O proximo de rede do 2W eh o gateway (2W na LAN crua; ts-gw alcancavel de ambos) —
por isso a escolha P1/P2 e local por maquina. P1 vs P2 segue criterio de latencia
(C2) no M3; ate la, P1 e o default e a rota viva no C1.

## Operacao

```
deploy/install.sh c1     # dev-workspace: usuario + code + /etc/navidrome-remote/env
                         #                + unidades (fake-mpv-sink + navidrome-remote)
deploy/install.sh sbc    # Orange Pi:     o mesmo com mpv real (navidrome-remote-mpv)
```

- `systemctl restart navidrome-remote` — o servico (Restart=always).
- `NVRA_MPV_SOCKET` aponta para o socke do motor: mock em C1, mpv real no 2W.
- Espiao: `$NVRA_SPY_FILE` (um NDJSON; fsync por linha).
- Credenciais (conta de servico Navidrome) vivem SO NO EnvironmentFile
  (`NVRA_NAV_USER`/`NVRA_NAV_PASS`); o install.sh NUNCA toca o arquivo se ja existe.

## Testes / suites

- `src/tests/test_http1_unit.py` — codecs chunked/gzip/deflate, HeadList, WireError.
- `src/tests/test_proxy_core.py` — matriz de aceite do proxy via FAKE upstream
  (302 raiz, Set-Cookie verbatim, CORS, bridge no HTML, espiao NDJSON,
  WebSocket 101, duplo-proxy, chunked/gzip, rewrite de `Location`).
- `src/tests/test_suite_a2.py` — Clausula B: espelhamento WS do espiao +
  sobrevivencia da sessao em `localStorage` no refresh (A2).
- `src/tests/test_spy_redaction.py` — redacao no chokepoint: segredos por nome
  e por forma nunca chegam ao NDJSON (A2.3).
- `src/tests/test_gateway.py` — gates L1/L2/L3 (presenca, anti-conta-de-servico,
  probe de vida por chamada, dedupe, fail-closed) (A3).
- `src/tests/test_api_m1.py` — `/api/v1/*` completo: validacoes, codigos,
  spy-pad, engine-down/idle (A3).
- `src/tests/test_controller_a4.py` — controller <-> `fake_mpv_sink`: acoes
  mapeadas para JSON-IPC, reconciliacao engine->store, shutdown/503 (A4).
- `src/tests/test_bridge_asset.py` — lint ESTATICO hermetico do `assets/bridge.js`:
  contrato de rotas, whitelist de `localStorage`, invariantes de credenciais,
  idempotencia (roda em qualquer lugar, sem browser) (A6).
- `suite/c1_probe.sh` — suite LIVE do M0' (evidencia LXC) e `suite/run_gate.py`
  — gate de upgrade ancorado (UI oficial) + `suite/player_boot_capture.py`
  (instrumentacao do boot real).
- `suite/bridge_e2e.py` + `suite/fixtures/bridge_ui/*.html` — MATRIZ
  BROWSER-LEVEL (A6): 15 cenarios em Chrome real (CDP) contra o trio hermetico
  (fake upstream + servico + fake mpv sink); exercita o pipeline exato
  browser -> proxy -> bridge -> gate -> controller -> mpv; auditoria com
  redacao aplicada; SKIP automatico sem Chrome (o wrapper unittest e
  `tests/test_bridge_a6.py`).

Rodando tudo (os testes sao um PACKAGE — descobrir a partir do `tests` exigindo
o top-level `src` via `-t`, senao os imports relativos quebram):

```
cd src
PYTHONPATH="$PWD" python3 -m unittest discover -s tests -t "$PWD" -v
python3 suite/bridge_e2e.py          # matriz browser-level (requer Chrome; dev-ws)
cd .. && cd suite && ./c1_probe.sh   # LIVE: requer alcance ao LXC (no C1: sempre)
```

## Layout

```
navidrome-remote/
├── proposals/                 # raciocinio de arquitetura (imutaveis apos decisao)
├── src/
│   ├── navidrome_remote/      # o servico unico (package + assets)
│   ├── tools/fake_mpv_sink.py # mock mpv (C1)
│   ├── tests/                 # unittest (stdlib, PACKAGE) — helpers dogfoodam o proprio http1
│   ├── suite/                 # c1_probe.sh (live) + run_gate.py + ws_probe.py +
│   │                          #   player_boot_capture.py + cdp.py/cdp_selftest.py (harness Chrome)
│   │                          #   + bridge_e2e.py & fixtures/bridge_ui/ (matriz A6)
│   └── docs/reports/          # artefatos gerados pelas suites (relatorio + spy)
├── deploy/                    # units systemd + env.example + install.sh
└── README.md                  # este arquivo
```

(suite/c1_probe.sh trata `src/` como raiz de operacao: `cd src && ./suite/c1_probe.sh`)

## Notas de seguranca (invariantes)

- Stream tokens e credenciais Navidrome NUNCA chegam ao navegador; vivem so no
  EnvironmentFile do servico. O bridge apresenta apenas a sessao da propria
  pagina (JWT nativo em `localStorage`, canal forte do gate) — nunca o login
  da conta de servico (que a camada L2 recusa por design) e nunca compoe/le
  `u/t/s/p` da pagina.
- O espiao registra NOMES de cookies, nunca valores — e desde A2.3 REDIGE no
  chokepoint de escrita (nomes e FORMAS de segredos), qualquer que seja o
  chamador; auditoria nunca vaza credencial.
- O unico ponto de injecao eh o `</head>` do HTML do upstream; a UI do Navidrome
  permanece intocada (Componente C: sem fork).
