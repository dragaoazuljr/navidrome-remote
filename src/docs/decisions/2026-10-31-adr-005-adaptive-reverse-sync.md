# ADR-005 — bridge.js: sync reverso ADAPTATIVO (política de polling)

- Data: 2026-10-31
- Status: Aceita (A7)
- Autor: Dev (carta t_923ef193)
- Afeta: `src/navidrome_remote/assets/bridge.js` (versão `0.3.0-a6` →
  `0.4.0-a7`; região polling + const `DIR_STALE_MS`),
  `src/tests/test_bridge_asset.py` (bump do pin de versão), esta ADR

## Contexto

A carta A7 pede: "Polling ADAPTATIVO de `/api/v1/status`; playback
comandado EXTERNAMENTE (operando outro cliente / o app no telefone
contra o mesmo SBC) reflete na UI sem corrida; estado visível
(faixa/tempo/posição) acompanha o engine (absorve o painel de status do
mapa v1); o polling falha SILENCIOSAMENTE (nunca degrada o player
nativo)."

Fatos fixados por ADRs anteriores que condicionam a decisão:

1. O player v0.64.2 NÃO tem canal push (ADR-002): o único meio de o
   browser descobrir mudanças do motor é o polling de
   `GET /api/v1/status`.
2. O A6 deixou o polling com cadência FIXA (1 s aba visível / 5 s
   oculta) e o resto da máquina (shadow clock, guard-write de
   elementos, locks direcionais `DIR_STALE_MS`/`PLAY_GHOST_GRACE_MS`,
   prova de paridade do stop via 2 amostras consecutivas, re-trigger
   F6d) validado pelos E2E T1-T15/16 — esses blocos NÃO se tocam.
3. A fix A6.R (PR #8, merge em `master` = `755eeede3f51`) recalibrado a
   região restore/unbind do ADR-004 (R1/R2) que é VIZINHANÇA desta
   mudança: o rework está vivo e os testes T10/T12/F6d são a âncora
   de regressão deste ADR.
4. O "painel de status do mapa v1" (faixa/tempo/posição dedicados) foi
   extinto no componente C ("sem fork"): a UI oficial É a superfície.
   Logo a absorção da A7 significa: garantir que faixa/tempo/posição
   do engine apareçam no player nativo — já projetados pelo A6; falta
   torná-los FIÁVEIS sob polling variável.
5. O `/status` do serviço reporta: disponibilidade do engine, estado
   de transport (playing/paused/stopped), posição, duração, volume e id
   da faixa corrente — o vetor mínimo que a UI projeta.

O problema concreto do polling fixo de 1 s em relação ao ACEITE:

- **Latência de reflexão externa insuficiente garantida?** Não — 1 s
  seria ótimo demais (custo de rede na família + latência do poll
  fazem o ciclo real sair de 1-2 s), e **parallax temporal**: com 2
  clocks a 1 Hz (browser shadow vs. amostra do status), projeções de
  posição podem chegar 0.5-1 s fora de ordem entre si durante
  transições (seek/vol/tracker-change), causando oscilações visíveis
  ("corrida" — o critério explícito do aceite).
- **Custo inelástico**: 1 s visível é desperdício em regime estável
  (estado idêntico repetido) e 5 s oculto pode ser grosso demais se o
  usuário volta à aba logo após um comando externo.
- **Falha do polling não modelada**: hoje o A6 soma tentativas
  fracassadas e mostra hint; A7 requer SEMÂNTICA de falha silenciosa:
  nunca degradar o player nativo, backoff que não martele a família,
  hint discreta só quando sustentado.

## Decisão

O polling passa de "intervalo fixo" a **laço auto-agendado** cuja
cadência é decidida POR TICK a partir do último estado observado.
Precedência (maior manda):

| # | condição                                    | período  | papel                          |
|---|---------------------------------------------|----------|--------------------------------|
| 1 | aba OCULTA (`document.hidden`)               | 5 s      | orçamento de fundo             |
| 2 | sequência de falhas de transporte            | 1→2→4→8→10 s (dobra, teto 10 s) | backoff; silencioso |
| 3 | borda de mudança do VETOR de estado (fast-ticks devidos) | 0.5 s × 4 ticks | modo de transição rápida |
| 4 | engine DESLIGADO (sessão válida)             | 3 s      | watcher de recuperação        |
| 5 | playing ESTÁVEL                              | 2 s      | nominal de reprodução          |
| 6 | paused / idle / parado ESTÁVEL              | 3 s      | nominal de descanso           |

Mecanismos:

### 1. Detecção de borda — o "vetor de estado"

Entre dois corpos de status CONSECUTIVOS aplicados, compara-se um
vetor reduzido (thresholds grossos de propósito, veja alternativas):

- estado de transport mudou (`playing ⇄ paused`, ambos ↔ parado);
- disponibilidade do engine conectou/desconectou;
- id da faixa corrente mudou (ou apareceu/despareceu) — auto-advance;
- salto de posição > 2.5 s entre amostras (sempre acima do avanço
  nominal de 2 s + margem de drift) OU de duração ≥ 2 s — seek externo
  ou carga de nova faixa;
- salto de volume ≥ 2 pontos — ajuste externo do volume.

Qualquer borda paga uma DÉBITA de `TRANS_FAST_TICKS = 4` ticks de
0.5 s (modo rápido): isso é o que faz um playback comandado
EXTERNAMENTE refletir na UI rapidamente sem correr — a transição é
amostrada a 2 Hz por exatos 2 s e depois o laço cai de volta à
cadência nominal. Ausência de borda simplesmente deixa a cadência
nominal rodar (drift normal de relógio entre amostras NUNCA qualifica
como borda: com ticks nominais de 2-3 s a deriva entre duas amostras
é menor que o threshold de 2 s na maioria dos casos, e quando
qualifica, custa 2 s extras de 0.5 s — inócuo).

A comparação roda NO CABEÇALHO de `applyStatus` (antes de qualquer
mutação de shadow), comparando contra o último corpo APLICADO
(`prevVec`) — as bordas são julgadas contra valores prévios.

### 2. Falha de polling = transporte ≠ sessão (silencioso)

Uma falha do `GET /status` divide em DUAS classes, como o gate já
divide:

- **Classe sessão (401/403)**: caminho DO GATE (ADR-004) — pode
  degradar (RESTAURO do player nativo é exatamente o comportamento
  desejado do T12). Nada muda daqui; o A7 nunca intercepta este ramo.
- **Classe transporte (5xx, timeout, rede caída — SBC offline no
  meio da sessão)**: SILENCIOSO por contrato — o ponte PERMANECE
  ENGAJADO (nenhum degrade), NENHUM elemento é tocado, só cresce o
  backoff de transporte (1 s dobrando até teto de 10 s — a família
  não é martelada; ~1 req/10 s no pior caso) e o contador
  `pollFailSince` marca o início da sequência. Só após 15 s contínuos
  de insucesso aparece o hint DISCRETO "SBC unreachable" (o badge
  que A6 já usava para indisponibilidade); UM SUCESSO limpa hint E
  backoff (contadores zerados) e o laço cai na cadência nominal.

Isso implementa literalmente o aceite "o polling falha
SILENCIOSAMENTE (nunca degrada o player nativo)": o player segue
tocando o que está tocando; o estado visível congela (a última
projeção vale) até o SBC voltar — nada pisca, nada quebra, nenhum
evento dispara.

### 3. Absorção do painel de status do mapa v1

Sem superfície nova (componente C: "sem fork", "não inventar UI"): o
badge existente do A6 + a projeção no player nativo (elemento
`<audio>` shadow-clock + campos faixa/tempo/posição que o A6 já
grava: título, dur, pos, volume, estado play/pause) COBRE o painel
dedicado que a spec v1 mapeara. A diferença do A7 é GARANTIR a
fiabilidade disso sob período variável: (a) o shadow clock interno
(interpolado a 1 s) continua responsável pela suavidade do tempo
visível ENTRE polls (o granulado do polling 2-3 s jamais aparece no
relógio on-screen); (b) os `seekTo`/botões continuam operando contra
a shadow (guard-write), então o usuário pode seguir interagindo
normalmente mesmo com cadência larga — a intenção viaja para o SBC e
o motor responde (o feedback chega no próximo poll, acelerado pela
borda de volume/pos).

### 4. Recalibração de horizons dependentes (impacto calculado)

O A6.2 calibrou dois horizontes assumindo polling de 1 s visível:
`DIR_STALE_MS = 2500` (ignorância de polls atrasados após uma
diretriz do usuário) e a janela de 2-amostras-da-paridade do stop do
A6.R. Com a cadência nominal MAIS LARGA (playing 2 s / paused 3 s),
o intervalo máximo entre amostras em regime estável VISÍVEL passou a
3 s (+ jitters de rede). Então:

- `DIR_STALE_MS`: 2500 → **3000** (cobertura do período mais largo
  visível + margem curta). Análise da JANELA de maior staleness:
  caso mais ruim = usuário dá play (direção registrada) e o poll
  anterior partiu no instante antes da borda; sob cadência 3 s ele
  só seria re-inspecionado em ≤ 3 s + 0.5 s (o primeiro fast-tick
  após a borda) = ≤ 3.5 s — o lock de 3 s cobre a quase-totalidade;
  o resíduo de 0.5 s é o MESMO que existia no 2500-vs-1 s original
  (mesma margem relativa), então nenhuma proteção do A6.2 perde força
  — ela foi recalibrada, não enfraquecida. Os demais lock
  (ghost-grace, window-jitter, debounce) não dependem do período do
  poll (dependem de tempos de UI humana) e ficam intactos.
- A prova de 2-amostras do stop (A6.R): continua valida sob fast-tick?
  Analisado: o STOP do motor produz borda de transporte (playing
  ativo → parado) e consequentemente fast-ticks 0.5 s — as 2
  amostras idênticas caem numa janela de ~1 s, MAIS RÁPIDO que o 1 s
  do A6. Sem mudança nesse teste.
- Regime OCULTO (5 s): o lock de paridade (T10) NUNCA precisou
  funcionar em aba oculta (nenhum elemento é escrito enquanto
  oculto — guard-writes são suprimidos; o estado só é reaplicado na
  visibilização). Mantém-se.

### 5. Telemetria de teste (seams read-only)

Dois seams introspectivos somam em `bridge._t` (mesmo padrão dos
existing do A6/A6.R): `_pollTimes()` (log circular dos últimos 8
timestamps de tick — permite aos E2E ASSERTIR os gaps reais da
cadência no Chrome real) e `_cadence()` (delay atualmente programado
+ streak de falhas + débito de fast-ticks). Somente leitura; nenhum
comportamento novo.

## Alternativas consideradas

- **Manter 1 s fixo visível e adicionar só o fast-mode** — rejeitado:
  mantém o custo inelástico e a parallax de 2 clocks 1 Hz durante
  transições, que é a raiz das "corridas" visuais que o aceite quer
  eliminar.
- **Canal push (WebSocket/SSE) server→browser** — rejeitado: ADR-002
  já fixou o transporte; abrir push exigiria um segundo serviço de
  longo-vida no SBC (mais superfície de falha, conflito com a
  filosofia "proxy estático + API REST" e com a rede da família).
- **Detector fino de mudança (qualquer delta, incluindo jitter de
  0.3 s de pos)** — rejeitado: o avanço NORMAL da posição entre duas
  amostras do regime playing (2 s) já seria classificado como borda —
  o fast-mode ficaria acionado permanentemente (= 2 Hz 24/7, pior que
  1 s fixo em QPS; constatado empiricamente no primeiro spot-check,
  antes do ajuste abaixo). Por isso o threshold de posição é
  ESTRITO > 2.5 s (sempre acima do avanço nominal + margem de
  drift): só um seek genuíno qualifica. Os demais vetores mantêm
  thresholds grossos (dur ≥ 2 s; volume ≥ 2 pts).
- **Exponenciar backoff sem teto (30 s, 60 s...)** — rejeitado: o
  hint de 15 s e a expectativa do usuário ("acabei de mandar algo
  pelo app") precisam de recuperação em segundos; teto 10 s preserva
  detecção de retorno do SBC em ~2 polls no pior caso.
- **Separar a política em config enviada do server** — rejeitado: YAGNI
  para 1 família; os 6 períodos são constantes locais com justificativa
  registrada aqui; tunagem posterior é trivial (1 commit).

## Consequências

- Positivas: latência de reflexão externa ~0.5-1 s após borda (vs.
  até 2 s+ com 1 s fixo sob jitter de rede); consumo em regime
  estável visível cai de 1 req/s para 0.5 req/s (2 s); falha de
  transporte degrada a requisição para 1/10 s com hint; o player
  nativo nunca degrada por saúde do SBC; o painel do mapa v1 fica
  efetivamente absorvido (fiabilidade documentada).
- Negativas/aceitas: o polling não é mais previsível em t (um
  debugador que olhe o QPS verá 0.33-2 Hz conforme o regime); o
  `DIR_STALE_MS` crescido 2500→3000 amplia em 0.5 s a janela em que
  um poll muito atrasado pode ser ignorado — aceitável (abaixo da
  percepção humana para playback contínuo e coberto pela análise §4);
  a telemetria de seams aumenta ligeiramente a superfície de leitura
  interna (read-only).
- Impacto nos testes: T1 (boot/engage) e T2/T3 (play/pause) exercitam
  o caminho nominal (2 s/3 s) e seguem passando por construção
  (tolerâncias deles têm folga de vários s); T11 (reverse-sync:
  seek/volume/retrigger/pause externos) é EXERCITADO pelafast-mode
  (tolerança 4-6 s do teste >> 2 s que o fast-mode entrega); T10/T12
  (A6.R) permanecem como guarda-regressão desta carta. E2E ganha
  coverage opcional via seams (`_pollTimes`/`_cadence`) para afirmar
  a cadência medida no Chrome real.

## Verificação

Executada (dev-ws, Chrome headless real):

Nota de revisão — o primeiro spot-check (antes da correção do
threshold) rodou com `>= 2 s` de posição e mostrou o fast-mode
ACIONADO PERMANENTEMENTE durante o playback contínuo (mediana dos
gaps ~0.56 s, ou seja 2 Hz, porque o avanço nominal de 2 s atingia
exatamente o limiar): o regime "playing estável" degenerava em
regime "transição". O spot-check serviu exatamente para isso:
evidenciou o caso-limite que a análise a priori subestimou.
Correção aplicada e re-validada: posição só qualifica como borda
se O MOVIMENTO supera 2.5 s (sempre acima do avanço nominal de
2 s + margem de drift), enquanto dur/volume conservam thresholds
grossos (2 s / 2 pts). Após a correção, o mesmo protocolo voltou
ao regime 2 s esperado.

1. `tests/test_bridge_asset.py` — pin de versão bump
   `0.3.0-a6` → `0.4.0-a7`; o resto do lint (guard, LS-whitelist,
   seams, tam < 80 k, node --check) segue verde sobre o asset novo.
2. Suite E2E completa (`bridge_e2e.py`): 15/15 esperados, com
   atenção especial a T11 (aceite desta carta) e T10/T12
   (regression-guard do A6.R).
3. Spot-check de cadência via seams: injetar um comando externo no
   SBC (via `rig.op`) e ler `_pollTimes` e `_cadence` para confirmar
   a transição 2 s → 0.5 s × 4 → 2 s.
4. Drill de falha silenciosa: derrubar o processo do service no meio
   da sessão (rig pode matar/reiniciar) e conferir: no element
   touched, backoff crescendo (1→10 s), hint "SBC unreachable" só
   após ~15 s, limpo na primeira retomada.
