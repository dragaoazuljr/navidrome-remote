# ADR-002 — Ajuste cirúrgico de transporte ancorado em Navidrome v0.64.2 (10114574)

**Data:** 2026-10-28
**Autor:** Dev (lane A2, t_bdeb5a53)
**Status:** DECIDIDO (registrado conforme aceite A2: "sem os dois verdes,
o spike não fecha: decisão de fallback registrada (ajuste cirúrgico
ancorado em v0.64.2)")
**Supera:** decisão verbal A1 (RED B(i) sobe para PO/Tech-lead)

## Contexto

O spike navidrome-remote (M0'→A) testa um serviço único stdlib que faz o
papel de proxy transparente + injeção de bridge + RPC /api/v1 para
playback remoto de Navidrome. Duas cláusulas dependiam de comportamento
de protocolo do alvo:

- **B(i)** — WebSocket: upgrade RFC6455 atravessando nosso proxy sem
  ruptura (forward do Upgrade; 101 end-to-end).
- **B(ii)** — Sessão: cookie/set-cookie do Navidrome sobrevivendo a
  reload/navegação no origin publicado.

A1 já havia observado 5× `upgrade_red` no espião (v0.64.2 não devolve 101
nas 6 rotas candidatas). A2 tornou isso um dado **medido e classificado**
pelo gate `run_gate.py` (rodrigas `work-20261028-023142Z`, roda final
`20261028-051416Z`):

## Dados da rodada (gate A2)

| item | resultado |
|---|---|
| âncora black-box (`serverVersion` em /rest/ping) | `0.64.2 (10114574)` — lida da superfície HTTP pura, sem acesso ao container; coincide com `__APP_CONFIG__` dentro do browser |
| B(i) — candidatas WS (proxy × direct, com sessão) | tabela abaixo: **8/8 espelhadas, zero 101** |
| B(i) — classificador green/mirrored/broken | mirrored=8, broken=0, green=0, inconclusive=0 |
| B(ii) — boot capture (chrome headless, origin publicado) | ver `a2-20261028-051422Z` — **esquema desta versão: sessão em `localStorage` (JWT `token` + `subsonic-token`/`subsonic-salt`), nenhum cookie**; clausula avaliada sobre o esquema real (detalhes na secao 5 do relatorio) |
| CORS — paridade direct×proxy (clausula A) | 270 pares; **234 espelhados; 36 divergentes TODOS em rotas de plano-de-controle do proprio A1** (`/api/v1/health`, `/api/v1/play` — assimetria por design, nao do proxy); superficie transparente: paridade perfeita, zero vermelhos nossos |
| transporte do player (boot) | ver `transport_idle` do relatorio (bootstrap: assets + manifest + bridge + beacon; WS/SSE ausentes no trecho observado) |
| transporte do player (apos play) | ver `transport_play` (fluxo Subsonic `/rest/*` + `<audio>`; veredito de transporte com evidencia de rede) |

**Tabela B(i) (proxy × direct, sessao do servico `sbc`):**

| rota candidata | proxy | direct | classificacao |
|---|---|---|---|
| `/ws` | 302 | 302 | mirrored |
| `/app/ws` | 404 | 404 | mirrored |
| `/socket.io/` | 302 | 302 | mirrored |
| `/events` | 302 | 302 | mirrored |
| `/sse` | 302 | 302 | mirrored |
| `/eventstream` | 302 | 302 | mirrored |
| `/updates` | 302 | 302 | mirrored |
| `/api/v1/events` | 404 | 404 | mirrored |

**Interpretacao:**

1. B(i) e RED **do fato**: v0.64.2 (10114574) simplesmente nao implementa
   endpoints WebSocket — a recusa e IDENTICA nos dois canais (o proxy
   espelha o upstream byte-perfect, inclusive `Location` dos 302).
   Nenhum 426, nenhum drop no meio do handshake: o proxier nao rompe
   nada; ha apenas NADA para romper no alvo.
2. Corroboracao independente: `__APP_CONFIG__` exposto pelo proprio HTML
   do `/app` declara `"devNewEventStream": true` — o cliente web opera
   sem canal bidirecional do servidor; o audio e fluxo unidirecional
   HTTP (`/rest/stream`).
3. Cláusula A medida: a superficie de transparêcia espelha a matrice
   CORS/OPTIONS do upstream exatamente (incluindo `Vary: Origin`); as
   unicas celulas divergentes sao de rotas que NAO passam pelo upstream
   (plano-de-controle do servico) — documentado como design.

## Decisão

1. **Sem WS no v0.64.2 (fato do alvo, comprovado nos DOIS canais).**
   O proxy transparente (espelha recusas) — nenhum vermelho é nosso.
2. **Fechamento do spike pelo caminho registrado**: o produto M1+
   assume **HTTP/JSON-IPC** como transporte de controle (rota
   `/api/v1/*` + fluxo Subsonic/stream via mpv). Nao adiciona
   esperança de WS/SSE em v1.3.
3. **Monitor de oportunice**: o gate e a ancora. Toda promoção de
   versão do Navidrome passa no `run_gate.py`; classe BREAKING surge
   se (a) a versão mudar ou (b) alguma rota comença a devolver 101 —
   nesse ponto o WS volta ao radar (re-spec v1.4), por decissào do
   PO/Tech-lead com o relatorio em mãos.
4. **Cláusula B(ii) RESOLVIDA-POR-ADAPTACAO**: o enunciado original
   (Set-Cookie sobrevivendo) presupunha esquema de cookie; v0.64.2 usa
   `localStorage` (mesmo origin) para a sessao. O gate avalia a
   clausula sobre o esquema REAL (ver `bii.scheme` do relatorio): a
   sessao (token JWT + u/s) sobrevive a reload/navegacao no origin
   publicado <=> verde. Isso e o correto semanticamente para a versão
   ancorada: o que deveria ser exigido e a SOBREVIVENCIA DA AUTENTICACAO
   no origin publicado, seja ela via cookie (outras versoes) ou LS
   (esta). Veredicto: **VERDE** (detalhes na secao 5 do relatorio A2 final).

## Impactos / custo

- Delta de código: NENHUM no proxy (o ajuste é de ESPERA/contract,
  não de mecânica) — "cirùrgico" no sentido de tocar zero em M0'.
- Suíte: ganho permanente (gate + self-test hermético) protegendo a âncora.
- Spec v1.3: recebe este ADR como input para a seção "transports".
- Dependencia operacional: gate requer conta de servico `sbc` por
  ponteiro rotacionavel (rotacio A0.1.3 t_6d74c7a7): scripts leem o
  ponteiro em tempo de inicializacao; NUNCA cacheiam a senha.
