# A2 — GATE DE VALIDAÇAO DE UPGRADE — 20261028-023142Z

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

Veredicto: **pendente-QA**

## 4. Boot do player (captura real, chrome headless)

- bootstrap indisponivel neste gate (None)

## 5. Baseline (diff vs execucao anterior)

Classe: **NONE**
- primeira fixacao (sem baseline anterior)

## 6. VEREDICTO FINAL

| item | valor |
|---|---|
| ancora v0.64.2 | OK |
| B(i) WebSocket | RED-facto (recusas espelhadas; v0.64.2 sem 101) |
| B(ii) sessao | pendente-QA |
| SPIKE CLOSED (green) | **NAO** |
| fechamento via fallback registrado | SIM → src/docs/decisions/2026-10-28-adr-002-fallback-transport.md |

> Sem os dois verdes, o spike fecha pelo CAMINHO REGISTRADO: ajuste cirurgico ancorado em v0.64.2 (transporte HTTP/JSON-IPC, sem esperanca de WS) — ver ADR-002.
