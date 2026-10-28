# A2 — GATE DE VALIDAÇAO DE UPGRADE — 20261028-051416Z

_Execucao: dev-ws x86 contra LXC 9102 (rota P1, .0/24). Ancora do spike: **v0.64.2**._

## 0. Ancora (black-box)

| fonte | valor |
|---|---|
| /rest/ping `serverVersion` | `0.64.2 (10114574)` |
| pagina / `__APP_CONFIG__.version` | `None` |
| coincide com 0.64.2? | **SIM** |

## 1. Cláusula B(i) — WebSocket atravessando o proxy

Contagens: green=`0` mirrored=`8` broken=`0` total=`8`

| rota candidata | via proxy | direct | classificacao |
|---|---|---|---|
| `/ws` | HTTP-302 (302) | - HTTP-302 (302) | upstream recusa nos dois canais (status 302) — recursão espelhada SEM ROMPIMENTO (não é vermelho nosso) |
| `/app/ws` | HTTP-404 (404) | - HTTP-404 (404) | upstream recusa nos dois canais (status 404) — recursão espelhada SEM ROMPIMENTO (não é vermelho nosso) |
| `/socket.io/` | HTTP-302 (302) | - HTTP-302 (302) | upstream recusa nos dois canais (status 302) — recursão espelhada SEM ROMPIMENTO (não é vermelho nosso) |
| `/events` | HTTP-302 (302) | - HTTP-302 (302) | upstream recusa nos dois canais (status 302) — recursão espelhada SEM ROMPIMENTO (não é vermelho nosso) |
| `/sse` | HTTP-302 (302) | - HTTP-302 (302) | upstream recusa nos dois canais (status 302) — recursão espelhada SEM ROMPIMENTO (não é vermelho nosso) |
| `/eventstream` | HTTP-302 (302) | - HTTP-302 (302) | upstream recusa nos dois canais (status 302) — recursão espelhada SEM ROMPIMENTO (não é vermelho nosso) |
| `/updates` | HTTP-302 (302) | - HTTP-302 (302) | upstream recusa nos dois canais (status 302) — recursão espelhada SEM ROMPIMENTO (não é vermelho nosso) |
| `/api/v1/events` *(rota local)* | HTTP-404 (404) | - HTTP-404 (404) | upstream recusa nos dois canais (status 404) — recursão espelhada SEM ROMPIMENTO (não é vermelho nosso) |

## 2. Cláusula A — mapa CORS/OPTIONS (paridade directo×proxy)

Pares: `270` | espelhados: `234` | divergentes: `36`

| rota | ctx | origin | acr | status D→P | diffs |
|---|---|---|---|---|---|
| `/api/v1/health` | anon | <none> | GET | 404→200 | status 404!=200; vary: ['Origin'] != None |
| `/api/v1/health` | anon | <none> | GET | 404→404 | vary: ['Origin'] != None |
| `/api/v1/health` | anon | <none> | POST | 404→404 | vary: ['Origin'] != None |
| `/api/v1/health` | anon | http://127.0.0.1:8481 | GET | 404→200 | status 404!=200; access-control-allow-origin: ['*'] != None; vary: ['Origin'] != None |
| `/api/v1/health` | anon | http://127.0.0.1:8481 | GET | 200→404 | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['GET'] != None |
| `/api/v1/health` | anon | http://127.0.0.1:8481 | POST | 200→404 | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['POST'] != None |
| `/api/v1/health` | anon | http://192.168.0.50:5173 | GET | 404→200 | status 404!=200; access-control-allow-origin: ['*'] != None; vary: ['Origin'] != None |
| `/api/v1/health` | anon | http://192.168.0.50:5173 | GET | 200→404 | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['GET'] != None |
| `/api/v1/health` | anon | http://192.168.0.50:5173 | POST | 200→404 | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['POST'] != None |
| `/api/v1/health` | session | <none> | GET | 404→200 | status 404!=200; vary: ['Origin'] != None |
| `/api/v1/health` | session | <none> | GET | 404→404 | vary: ['Origin'] != None |
| `/api/v1/health` | session | <none> | POST | 404→404 | vary: ['Origin'] != None |
| `/api/v1/health` | session | http://127.0.0.1:8481 | GET | 404→200 | status 404!=200; access-control-allow-origin: ['*'] != None; vary: ['Origin'] != None |
| `/api/v1/health` | session | http://127.0.0.1:8481 | GET | 200→404 | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['GET'] != None |
| `/api/v1/health` | session | http://127.0.0.1:8481 | POST | 200→404 | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['POST'] != None |
| `/api/v1/health` | session | http://192.168.0.50:5173 | GET | 404→200 | status 404!=200; access-control-allow-origin: ['*'] != None; vary: ['Origin'] != None |
| `/api/v1/health` | session | http://192.168.0.50:5173 | GET | 200→404 | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['GET'] != None |
| `/api/v1/health` | session | http://192.168.0.50:5173 | POST | 200→404 | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['POST'] != None |
| `/api/v1/play` | anon | <none> | GET | 404→404 | vary: ['Origin'] != None |
| `/api/v1/play` | anon | <none> | GET | 404→404 | vary: ['Origin'] != None |
| `/api/v1/play` | anon | <none> | POST | 404→404 | vary: ['Origin'] != None |
| `/api/v1/play` | anon | http://127.0.0.1:8481 | GET | 404→404 | access-control-allow-origin: ['*'] != None; vary: ['Origin'] != None |
| `/api/v1/play` | anon | http://127.0.0.1:8481 | GET | 200→404 | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['GET'] != None |
| `/api/v1/play` | anon | http://127.0.0.1:8481 | POST | 200→404 | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['POST'] != None |
| `/api/v1/play` | anon | http://192.168.0.50:5173 | GET | 404→404 | access-control-allow-origin: ['*'] != None; vary: ['Origin'] != None |
| `/api/v1/play` | anon | http://192.168.0.50:5173 | GET | 200→404 | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['GET'] != None |
| `/api/v1/play` | anon | http://192.168.0.50:5173 | POST | 200→404 | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['POST'] != None |
| `/api/v1/play` | session | <none> | GET | 404→404 | vary: ['Origin'] != None |
| `/api/v1/play` | session | <none> | GET | 404→404 | vary: ['Origin'] != None |
| `/api/v1/play` | session | <none> | POST | 404→404 | vary: ['Origin'] != None |

## 3. Cláusula B(ii) — sessão sob reload/navegacao no origin publicado

Veredicto: **VERDE**

| passo | auth | form voltou? | cookies apos (nomes) |
|---|---|---|---|
| reload-1 | None | False | - |
| reload-2 | None | False | - |
| nav-raiz | None | False | - |
| nav-app | None | False | - |

## 4. Boot do player (captura real, chrome headless)

- login: True (formulario)
- rotas no boot: /rest/getCoverArt, http://127.0.0.1:8481/1682c5fc-c683-4665-aeca-ca09d27ee134, http://127.0.0.1:8481/7fb21d54-412d-4dec-9978-6f18304002f4, http://127.0.0.1:8481/8069a683-8449-40b9-ae2f-b2a365e44ec5, http://127.0.0.1:8481/8f0a096c-a4db-4124-a201-7d4a01e12b9d, http://127.0.0.1:8481/acfddde3-aa3d-4f52-9070-fb76864c1370, http://127.0.0.1:8481/b67726f6-5c06-4dfe-b6e2-3e2efd5a0f78, http://127.0.0.1:8481/bd0049ae-0574-45b1-9abe-3c5a0ba3e709, http://127.0.0.1:8481/d7a4c176-9a4e-437b-9057-6dd4c654475b
- transporte em repouso: INCONCLUSIVO neste trecho (tocar a faixa?)
- transporte apos play: INCONCLUSIVO neste trecho (tocar a faixa?)
- bridge.js executou no navegador: True

## 5. Baseline (diff vs execucao anterior)

Classe: **MAJOR**
- bii: pendente-QA -> VERDE

## 6. VEREDICTO FINAL

| item | valor |
|---|---|
| ancora v0.64.2 | OK |
| B(i) WebSocket | RED-facto (recusas espelhadas; v0.64.2 sem 101) |
| B(ii) sessao | VERDE |
| SPIKE CLOSED (green) | **NAO** |
| fechamento via fallback registrado | SIM → src/docs/decisions/2026-10-28-adr-002-fallback-transport.md |

> Sem os dois verdes, o spike fecha pelo CAMINHO REGISTRADO: ajuste cirurgico ancorado em v0.64.2 (transporte HTTP/JSON-IPC, sem esperanca de WS) — ver ADR-002.
