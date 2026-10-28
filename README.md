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
| `navidrome_remote/api.py` | `/api/v1/*` (health/status/play/seek/pause/next/prev/volume/stop/spy). Em M0' o `play` recebe URL ja montada OU Subsonic `id` + credencial do lado servidor (NUNCA no navegador — invariante). |
| `navidrome_remote/ipc.py` | cliente mpv: socket AF_UNIX, JSON-NL, correlacao por `request_id`, eventos soltos para hook. |
| `navidrome_remote/spy.py` | espiao NDJSON append+fsync: toda requisicao/resposta/cookie(nome)/upgrade/erro. Nomes de cookies SIM, valores NUNCA. |
| `navidrome_remote/assets/bridge.js` | o app: intercepta trafego (fetch/XHR/sendBeacon/WebSocket) e beacons a telemetria do playback para `/api/v1/spy`. Metadados de compatibilidade: anchor v0.64.2. |
| `navidrome_remote/config.py` | 100% EnvironmentFile (`NVRA_*`); falha rapido (stderr + exit!=0) se mal configurado. |
| `navidrome_remote/__main__.py` | entrypoint `python3 -m navidrome_remote` (banner classifica a rota P1/P2/custom). |
| `tools/fake_mpv_sink.py` | MOCK do mpv para C1 (mesmo contrato IPC; `--null-audio` equivalencia) — permite exercitar play/seek/volumen/a sem hardware de audio. |

Sem session-gate, sem fila, sem persistencia e sem interceptador de transportes no
`bridge.js`: isso chega em M1 (escopo deliberado de M0', declarado no PR).

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
- `src/tests/test_proxy_core.py` — matriz de aceite via FAKE upstream (dogfood de `http1`):
  transparencia (302 raiz, Set-Cookie verbatim, OPTIONS/CORS), bridge no HTML,
  espiao NDJSON (boot + upgrade_red/red), WebSocket (echo verde + recusa vermelha),
  duplo-proxy idempotente, chunked/gzip, rewrite de `Location`.
- `src/tests/test_api_ipc.py` — `/api/v1/*` end-to-end contra o `fake_mpv_sink.py` REAL
  (subprocess, socket em `/dev/shm`): play->status->pause->seek(42)->next->volume->stop,
  validacoes 400/404/501(engine-down).
- `suite/c1_probe.sh` — suite LIVE do M0': sobe servico+mock, dispara as sondas da
  proposta §6 contra o LXC 9102 e gera `docs/reports/<data>-m0-prime-c1.md`
  (+ copia do spy). Roda SEM root — e o artefato de evidencia para o PR.

Rodando tudo (os testes sao um PACKAGE — descobrir a partir do `tests` exigindo
o top-level `src` via `-t`, senao os imports relativos quebram):

```
cd src
PYTHONPATH="$PWD" python3 -m unittest discover -s tests -t "$PWD" -v
cd ../suite && ./c1_probe.sh          # requer alcance ao LXC (no C1: sempre)
```

## Layout

```
navidrome-remote/
├── proposals/                 # raciocinio de arquitetura (imutaveis apos decisao)
├── src/
│   ├── navidrome_remote/      # o servico unico (package + assets)
│   ├── tools/fake_mpv_sink.py # mock mpv (C1)
│   ├── tests/                 # unittest (stdlib, PACKAGE) — helpers dogfoodam o proprio http1
│   ├── suite/                 # c1_probe.sh + ws_probe.py + _digest.py
│   └── docs/reports/          # artefatos gerados pelas suites (relatorio + spy)
├── deploy/                    # units systemd + env.example + install.sh
└── README.md                  # este arquivo
```

(suite/c1_probe.sh trata `src/` como raiz de operacao: `cd src && ./suite/c1_probe.sh`)

## Notas de seguranca (invariantes)

- Stream tokens e credenciais Navidrome NUNCA chegam ao navegador; vivem so no
  EnvironmentFile do servico.
- O espiao registra NOMES de cookies, nunca valores.
- O unico ponto de injecao eh o `</head>` do HTML do upstream; a UI do Navidrome
  permanece intocada (Componente C: sem fork).
