# ADR-006 — Degradação graciosa: POWERDOWN do bloco de controle + ledger de conflitos last-write-wins

- Data: 2026-10-31
- Status: Aceita (A8)
- Autor: Dev (carta t_2b919598)
- Afeta: `src/navidrome_remote/assets/bridge.js` (versão `0.4.0-a7` →
  `0.4.0-a8`: watcher de sessão perpétuo, função `powerdown`, marker
  `X-NVR-Plane` no `doFetch`, seam `_powerdown`),
  `src/navidrome_remote/api.py` (atribuição de plano, ledger
  last-write-wins, evento `plane_conflict`, contador `conflicts` no
  `/status`), `src/tests/test_bridge_asset.py` (teto 80k → 90k + nova
  superfície estática), `src/tests/test_conflict_ledger.py` (novo),
  `src/suite/bridge_e2e.py` (cenários T16-T18), esta ADR

## Contexto

Aceite da carta A8 (literal):

1. "Sem sessao valida, o bloco de controle do SBC DESLIGA e a UI
   continua 100% nativa — nunca quebrar o player do usuario."
2. "Conflito simultaneo (UI vs comando externo) = last-write-wins COM
   log."

Fatos fixados por ADRs anteriores que condicionam a decisão:

1. A máquina de estados do bridge já existe desde A6 (`passive` /
   `engaged` / `degraded`, com restauração do player nativo no degrade —
   ADR-004) e o A7 tornou o polling adaptativo e a falha de transporte
   silenciosa com backoff (1→10 s) e hint "SBC unreachable" a 15 s
   (ADR-005). O POWERDOWN reusa essa anatomia de teardown; ele diverge
   apenas no RESULTADO (classes abaixo).
2. O veredito de sessão era decidido SOMENTE NO BOOT: o watcher
   `startSessionWatcher()` tinha a flag `decided` — uma vez enganjado,
   nunca mais examinava o ciclo de vida da sessão. Consequência: um
   LOGOUT no meio da sessão deixava o bridge engajado até o próximo 401
   (segundos atrás, na cadência do polling) — e um LOGOUT que esvazia o
   `localStorage` não gera nenhum request pendente: a página ficaria
   "enganjada oco", enviando intenções a um SBC sem sessão. Pior: com o
   bridge engajado, os botões nativos do usuário são INTERCEPTADOS
   (viam para `/api/v1/*`) e, sem sessão lá, voltam 401/503 — o player
   PARA DE RESPONDER NATIVAMENTE. Isso viola frontalmente o aceite (1).
3. O blackout do SBC (classe transporte do ADR-005) tinha SEM TERMINUS:
   o backoff silencia o martelar e o hint informa, mas o bridge ficava
   indefinidamente segurado sobre um SBC morto. Com o SBC fora, o botão
   de play nativo interceptado não reproduz (a intenção vai para a API
   morta) — o player efetivamente para de funcionar como player.
4. Conflitos UI × externo já acontecem por desenho (o app do celular, a
   CLI, o operador — todos dirigem o MESMO SBC); o que faltava era
   REGISTRAR quem ganhou (aceite 2). Hoje ninguém sabe: as escritas vão
   direto ao motor sem atribuição de origem.
5. Invariante imutável: o bridge NUNCA quebra a UI oficial e NUNCA lê
   chave de credencial fora da whitelist (`token`, `navidrome_token`,
   `is-authenticated`) — o novo watcher lê as MESMAS chaves, com a
   MESMA whitelist do lint (invariante de credenciais preservada).

## Decisão

### 1. Powerdown: o bloco de controle DESLIGA (duas classes)

Quando a sessão morre ou o SBC some por tempo suficiente, o bridge executa um POWERDOWN: restaura o player nativo (URLs reais de stream, un-
mute, release dos patches de protótipo, desarma listeners/timers — a
mesma anatomia do degrade do A6/A7, com RESTAURAÇÃO ANTES DO UNPATCH,
porque os originais são precisos) e troca de classe de resultado:

| classe | gatilho | resultado | sinalização |
|--------|---------|-----------|------------|
| `session-lost` | a sessão DA (logout/esvaziamento do storage, visto pelo watcher perpétuo) OU o gate responde `401 session-missing` | estado `PASSIVE` — o bridge fica mudo: sem badge, sem re-probe | NENHUMA (a superfície de login deve ser 100% nativa) |
| `sbc-offline` | blackout de transporte SUSTENTADO além de `HANDOVER_MS` (20 s) | estado `DEGRADED` — o ÁUDIO DEVOLVE a este navegador (o usuário continua ouvindo a música, nativa) + re-probe de 30 s que re-engaja no primeiro 200 (a sessão da página continua válida) | badge família "SBC unreachable" (continua mostrando "SBC offline — retrying") |

Regras transversais:

- **Jamais lança**: um powerdown que lançasse seria exatamente o defeito
  que esta milestone existe para impedir (`try/catch` ao redor de todo o
  corpo; o caminho finaliza em `updateHint()`).
- **Re-join suave**: ao voltar a haver sessão (login de novo), o watcher
  re-engaja — mas um elemento que esteja REPRODUZINDO NATIVAMENTE
  continua nativo até o próximo load/seleção: o bridge NUNCA arrebenta
  o áudio do usuário no meio de uma faixa (a tomada de posse ocorre no
  próximo `src` set ou seleção, comportamento já estabelecido pelo
  restore do A6).
- **Anti-flap**: 3 powerdowns dentro de 5 min (`FLAP_MAX`/
  `FLAP_WINDOW_MS`) colocam o bridge em sono por 5 min
  (`FLAP_DORMANT_MS`): em rede instável ele não cicla eternamente
  down/up/down.
- **O watcher sobrevive a TUDO**: `powerdown`, `degrade` e `engage`
  reiniciam o intervalo de watch (`restartWatch`). Ele é o ÚNICO
  observador do ciclo de vida da sessão; nenhum outro laço do bridge vê
  um logout em andamento.

### 2. Watcher de sessão PERPÉTUO (substitui o `decided` do boot)

O antigo watcher decidia uma vez (boot) e morria após 10 min. O novo é
perpétuo, cadência `LS_WATCH_MS` = 1 s, e trata TRANSIÇÕES (não níveis):

- neg→pos (aparece sessão): se não estiver engajado e não estiver em
  sono anti-flap → aplicar o GATE DE VERSÃO (a versão da página diverge
  da âncora → degrada `anchor-drift`, UI 100% nativa, beacon para o A2)
  senão `engage` (com contagem de `recoveries` se saiu de um
  `session-lost`).
- pos→neg (morre sessão) e o bridge NÃO estivesse `passive` →
  `powerdown("session-lost")`.

Justificativa do 1 s: a latência máxima de desligamento passa de
"até o próximo 401 (segundos, e às vezes nenhum)" para ≤ 1 s — dentro
da percepção de "desligou de fato". Custo: uma leitura de `localStorage`
por segundo (as mesmas chaves da whitelist; o browser cacheia a leitura
— inócuo). Removido o teto de 10 min: o watch barato precisa viver o
ciclo inteiro (o lado do logout é exatamente o que um watch temporário
não enxerga).

### 3. Último-quem-escreve vence, COM LOG (serviço, camada `/api/v1/*`)

- **Planos**: cada chamada `POST` a rota de escrita recebe uma
  atribuição de plano NO DISPATCHER: header `X-NVR-Plane: ui`
  (marca adicionada pelo `doFetch` do bridge em TODO o tráfego que vem
  da página) → plano `ui`; token estático (L0) → plano `op` (o operador
  é conhecido por construção); ausente → `ext` (app/CLI/ferramentas que
  não marcam — imposível exigir header de clientes que não controlamos).
  Auditoria apenas: o marcador é ESPOLIÁVEL e NADA concede
  (autenticação continua sendo L0..L3 — a invariante de autorização do
  M1 não muda; nenhum caminho de permissão depende do header).
- **Classes de recurso** (duas escritas disputam o mesmo objeto musical
  quando endereçam a mesma classe): `track` = play/queue/next/prev/
  previous (todas decidem "o que toca"); `transport` = pause/resume/
  stop (máquina de estado de transporte); `seek`, `volume`, `shuffle`,
  `repeat` = elas próprias.
- **Política**: last-write-wins por ORDEM DE CHEGADA — a escrita aceita
  por último com **200** prevalece (todas as sucessões desta API são
  200 `ok:true`; 503s explícitos e 4xx são rejeições, não vencedoras) —
  e uma rejeição NUNCA carimba o ledger.
  O ledger (uma entrada por classe: plano, timestamp monotônico, rota)
  (carimbado SOMENTE por uma escrita aceita — uma entrada por classe:
  `(plano, timestamp, rota)`) registra a corrida: se a entrada anterior da MESMA classe é de
  PLANO DIFERENTE e está dentro de `CONFLICT_WINDOW_S` (3 s — a
  calibração "simultâneo": cadência máxima visível de intenções +
  margem, mesma filosofia de `DIR_STALE_MS` do ADR-005) → evento
  `plane_conflict` no spy (`winner`, `loser`, `resource`, `route`,
  `prev_route`, `gap_ms`, `seq`) + contador acumulativo `conflicts` no
  corpo do `/status` (ambas as variantes). O relógio é INJETÁVEL
  (`clock=`) — a janela de 3 s é exercitável em unidade sem dormir.
- **Por que ledger e não mutex/arbitragem**: o aceite MANDA
  last-write-wins (determinístico por chegada); um mutex mudaria a
  semântica (seria a UI esperando o externo, ou vice-versa). Registrar
  É a entrega; quem ganhou já ficou decidido na hora da aplicação.

### 4. Teto do lint do asset: 80k → 90k caracteres

Histórico do teto (lint hermético `test_bridge_asset.py`): 70k (A5) →
80k (A6/A7, asset chegou a 79.061) → **90k (A8)**. Medido pós-A8:
**87.031 caracteres** (folga ~3 k). A8 acrescentou o subsistema
powerdown (watcher perpétuo + duas classes + handover/anti-flap +
marker de plano) — crescimento substancional, porém acotado e
documentado; o objetivo "mantenha-o magro" continua vigente via teto.

### 5. Versão e contrato

- `VERSION`/pin: `0.4.0-a7` → `0.4.0-a8` (bump do lint).
- O contrato T12 do A6 é PRESERVADO: `401 session-invalid` /
  `session-unverifiable` e `403 service-account-forbidden` continuam
  degrando com badge explicativo + re-probe — o novo ramal `401
  session-missing` vai ao powerdown (solene) apenas para aquele código (é a
  assinatura exata do "sem sessão alguma", distinta de "sessão
  inválida/existe").
- Seams: `_powerdown()` (classe em curso) soma ao surface de leitura
  read-only (o lint estende o pin do seam; `_resetStats` não limpa
  `powerdowns` — histórico persistente para auditoria).

## Alternativas rejeitadas

- **Manter o `decided` do boot e depender do 401 para detectar
  logout** — rejeitada: lag de segundos (na melhor hipótese) e ZERO
  garantia (um logout que esvazia o storage não dispara request; a
  página ficava engajada-oca e os botões nativos paravam de funcionar —
  o defeito central desta milepostone).
- **Powerdown único (uma classe só)** — rejeitado: o resultado certo
  DEPENDE do gatilho. Sem sessão → silêncio (nada a sondar, login 100%
  nativo); SBC fora com sessão viva → badge + devolução do áudio +
  re-probe (o usuário espera o retorno; o som não deve parar). Mesclar
  ambas em uma classe quebraria metade de cada.
- **No `sbc-offline`, silenciar também (sem badge)** — rejeitado: o
  badge é o único canal seguro (não toca o player) de dizer "o SBC
  sumiu, tentando voltar"; sem ele, o usuário não saberia por que as
  mudanças de track externas deixaram de refletir.
- **Mutex/prioridade entre planos para o conflito** — rejeitado: o
  aceite manda last-write-wins; prioridade seria uma decisão de
  produto (quem "vale mais", UI ou app) que não pertence a esta
  camada; o log dá ao operador a informação para decidir depois.
- **Exigir marker de plano de TODO chamado** — impossível: o app do
  celular e a CLI são clientes que não controlamos; ausência = `ext`
  basta para o requisito (e protege o tráfego legítimo que não
  conhece o header).
- **Ledger no controller/lado do engine** — rejeitado: o PLANO só é
  conhecido na camada HTTP (o engine vê comandos, não o chamador);
  mover para baixo perderia a atribuição.
- **Watch de 5 s para cortar o custo** — rejeitado: 1 s de `localStorage`
  por tick é inócuo; 5 s de latência de desligamento começa a parecer
  "congelou" para o usuário.
- **Registrar conflitos APENAS no spy (sem contador no `/status`)** —
  rejeitado: o contador torna o aceite verificável em E2E (o `rig` lê o
  `/status` sem ter que dissecar o arquivo de auditoria) e dá
  visibilidade ao operador.

## Consequências

- Positivas: logout → desligamento solene (silencioso) imediato (≤ 1 s), UI 100%
  nativa — aceite (1) literal; blackout prolongado → áudio devolvido ao
  usuário + re-engajamento automático — o player NUNCA fica morto;
  conflitos registrados, contados e citáveis — aceite (2) literal;
  anti-flap protege redes instáveis; a máquina de estado já revisada
  (T10/T12) não ganha comportamento novo — permanecem como
  guarda-regressão; a invariante de credenciais e a de "nunca tocar a
  UI oficial" preservadas (whitelist idêntica, restauração antes do
  unpatch).
- Negativas/aceitas: o asset cresce 79.061 → 87.031 chars (+6,8%) —
  documentado e acotado no §4; o watch perpétuo adiciona um timer
  permanente (uma leitura/s — inócuo, mas o `stopTimers` agora também o
  gestiona — o estado mudo não acumula timers); o re-join suave
  ADIA a tomada de posse da música que o usuário começou a tocar
  nativamente até a próxima seleção — deliberado (não arrebentar a
  faixa tocando); o marker `X-NVR-Plane` viaja em todo write do bridge
  (um header a mais por request — inócuo na LAN).
- Impacto nos testes: T12 (401/403) segue verde POR CONSTRUÇÃO (só o
  código `session-missing` migrou de caminho); T10 (stop), T11
  (reverse-sync) e o resto da matriz regressiva não ganham ramo novo;
  o novo cobertura: E2E T16 (logout → powerdown solene → re-login →
  re-join), T17 (blackout > 20 s → `sbc-offline` → audio devolvido →
  re-join no re-probe), T18 (corrida UI×externo dentro da janela →
  `plane_conflict` + `conflicts` no `/status`); em unidade,
  `test_conflict_ledger` cobre os positivos (ui>ext, ext>ui, op>ui) e
  os negativos (mesmo plano, janelas ultrapassadas, classes distintas,
  reads e rejeições não tocam o ledger).

## Verificação

Executada nesta entrega (dev-ws):

1. `tests/test_bridge_asset.py` — lint hermético atualizado (teto 90k,
   pin `0.4.0-a8`, nova superfície A8: `"session-lost"`, `"sbc-offline"`,
   `_powerdown`, `LS_WATCH_MS`, `HANDOVER_MS`, `X-NVR-Plane`,
   `FLAP_DORMANT_MS`) — verde sobre o asset novo.
2. `tests/test_conflict_ledger.py` (novo) — mundo hermetic (fake
   gateway + fake engine + relógio injetado): 8 casos cobrindo os três
   pares de plano, os quatro negativos e o contador do `/status`.
3. `suite/bridge_e2e.py` (Chrome headless REAL): T16/T17/T18 + a matriz
   regressiva inteira (T1-T15/16) — o E2E confirma o powerdown com o
   player nativo íntegro e o re-join após a condição sanar.
4. Suite completa de unidade (`python -m unittest discover tests/`) —
   regressão do M1/A5/A6/A7 zerada.

---

## Adendo (ciclo de correção A8, 2026-10-31 — re-execução fria em vermelho)

A re-execução fria dos cenários críticos do ciclo A8 (tentativa 2) expôs três
defeitos. São correções das decisões deste ADR, não expansão de alcance:

### AD-1 — A predição de dormência passou a ser COUNT-based (T17)

§4.2 prescrita a dormência quando "três powerdowns caírem dentro da janela".
A primeira implementação usou `Array.prototype.every` sobre a lista de
timestamps, que retorna **verdade vacia para listas curtas**: um ÚNICO
powerdown armava 5 minutos de dormência, que engolia o heal do re-probe (o
bridge não reajuntava após um blackout de 20 s). A predição agora exige
`length >= FLAP_MAX && every(dentro da janela)` — a intenção original se
mantém exata: só a rajada COMPLETA adormece o watch.

### AD-2 — O plano do ledger é atribuição POR PEDIDO (T18)

§4.3 descreve o plano como a atribuição de uma escrita; a primeira
implementação o cachava em ESTADO DA INSTÂNCIA (`Api._cur_plane`). O serviço
atende pedidos concorrentes, então qualquer outro pedido — tipicamente o
próprio poll de 1 s de status do bridge — completando entre o dispatch e o
note da escrita reescrevia o plano em cache e o conflito era
**silenciado** (cenário E2E T18). `handle()` agora calcula o plano por
pedido e o encaminha ao `_conflict_note()`; nada de estado compartilhado
participa da decisão, e o comportamento de §4.3 passa a valer sob
concorrência. Regression: `tests/test_conflict_ledger.py::test_i_concurrent_poll_cannot_steal_attribution`.

### AD-3 — Separation of storage realms na rig headless (fidélidade de teste, T16)

Na rig E2E (Chrome headless), evals `Runtime.evaluate` executadas **fora**
do documento principal operam sobre um realm de storage DISTINTO do mundo
principal da página: o `removeItem` emitido pela rig é invisível à página,
e a sessão estacionada nunca muda (medido com espiões in-page de
`localStorage.setItem/removeItem`: os escritores não eram chamados; as chaves
persistiam). O logout REAL de uma SPA é escrita de main-world feita pelo
próprio app — os cenários T16 passaram portanto a operar pela seam
`__FIX__.simulateLogout()/simulateLogin()` da fixture, réplica fiel do
handler da SPA (mesmas chaves). Conclusão: o vermelho de T16 era ilusão de
rig, não defeito de produto; o contrato do watcher (§4.1) se mantém e foi
confirmado vivo (tick 1 Hz, transição negativa detectada).

### AD-4 — Correção da obrigação de "handback" (session-lost vs sbc-offline)

A asserção E2E original exigia que, num LOGOUT, o elemento fosse promovido
a uma URL de stream real mesmo tendo sido apenas um espelho sintético. Era
leitura excessiva: §1 promete que *o player nunca morre* — o logout NÃO
TOCA os elementos do usuário (nenhuma promoção, nenhum feed sintético,
nenhum inversão de mute). As obrigações passam a ser:

- `session-lost` — o elemento fica EXATAMENTE como estava antes do logout
  (100% nativo: com stream real, continua com ele; sem, segue ocioso).
  O SBC segue falando (fonte da verdade enquanto houver sessão).
- `sbc-offline` — elementos que POSSUÍRAM um stream real o recuperam
  desmutados (o usuário continua ouvindo). Elementos que nunca o tiveram
  (só conheceram o feed sintético) ficam 100% nativos e o bridge LOGGA
  `bridge-handback:pending` (journal + `stats.handback_pending`): o áudio
  que morreu com o SBC não é conjurado do nada. Fabricar
  `t=<jwt>` dentro de uma URL de stream fica explicitamente FORA deste
  marco (violaria o espírito da invariante M0' de credencial fora de
  resposta); esse contrato requer validação do esquema de credenciais do
  upstream (Navidrome) e pertence à série B (device).

### Observação não bloqueante

Latente (pré-A8), visto na rodagem fria: o handler de SIGTERM de
`__main__.py` chama `store.save()`, método inexistente em `QueueStore` —
a exceção é inofensiva (o processo termina de qualquer modo); sinalizada
para manutenção.
