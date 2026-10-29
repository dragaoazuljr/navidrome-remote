# ADR-004 — bridge.js: intercepção dos eventos nativos do player

- Data: 2026-10-29
- Status: Aceita (A6)
- Autor: Dev (carta t_59b6628a)
- Afeta: `src/navidrome_remote/assets/bridge.js` (reescrito),
  `src/suite/fixtures/bridge_ui/*`, `src/suite/bridge_e2e.py`,
  `src/tests/test_bridge_asset.py`, `src/tests/test_bridge_a6.py`,
  extensão de `src/tests/helper_upstream.py`

## Contexto

O cartão A6 pede: "hooks nos controles da UI oficial (play/pause,
next/prev, seek, shuffle, repeat, volume, seleção de faixa) disparam
`/api/v1/*` COM a sessão; operando a UI, o playback responde
corretamente". A carta A3 entregou `/api/v1/*` atrás do gate de
sessão (L0 estática opcional, L1 presença, L2 anti-conta-de-serviço,
L3 probe por chamada). Falta o elo browser→serviço.

Fatos fixados pelas medições anteriores (M0'/A2/A3) que condicionam
a decisão:

1. O player v0.64.2 NÃO tem canal push: nenhum WebSocket/SSE de
   transporte de mídia; o estado vive no DOM (elemento `<audio>`,
   sliders, botões com `aria-label`/classe de ícone).
2. A sessão do browser é o JWT nativo estacionado em
   `localStorage` (chave `token`), sem cookies de autenticação
   (cláusula B(ii)).
3. O transporte de áudio é a URL Subsonic
   `GET /rest/stream?id=...&u=...&t=...&s=...` (unidirecional,
   com `Range`), carregada no `<audio>` pelo bundle oficial.
4. O espíão (A2/A2.3) aplica redação no chokepoint de escrita
   (nomes e formas de segredo nunca chegam ao NDJSON); a casa
   inteira segue a invariantes de credenciais.

## Decisão

O A6 reescreve o asset como um **interceptor de fronteira de mídia +
roteador de intenções**, em três blocos cooperativos:

### 1. Fronteira de mídia (o "swap silencioso")

A captura é feita na ÚNICA fronteira onde o browser consome o
upstream: o atributo/propriedade `src` (e `srcObject`) do
`HTMLMediaElement`. Enquanto ENGAGED, toda URL que clasifica como
stream Navidrome (`/(rest/)?stream?...id=...`) é SUBSTITUÍDA por um
feed de SILÊNCIO gerado em memória (WAV zero, 8 kHz, 0.25 s, em
loop) — o elemento "toca" silêncio (o usuário não ouve nada local),
nenhuma requisição de áudio vai ao upstream (zero banda na família)
e o id da música é embarcado em `POST /api/v1/play {id}`. O motor
mpv (no SBC) consome a stream REAL com a conta de serviço
(já é assim desde A4; o URL composto nunca atravessa para o
browser).

Escolhas específicas:

- **Patch de protótipo somente enquanto ENGAGED**: os originais
  são salvos e restaurados na degradação — em modo nativo o
  browser é bit-a-bit igual ao stock (nenhum efeito lateral
  permanente).
- **Interceptação também em `setAttribute("src", ...)`**: alguns
  bundles atribuem `src` via setAttribute (que em engines
  antigos ignora o setter de propriedade para o algoritmo de
  carga). Ambas as portas convergem no mesmo classificador.
- **`loop=true` + `muted=true`** no elemento silenciado: evita
  `ended` em cascata (auto-advance fantasma do player) e qualquer
  som local residual.
- **URL de síntese interna** (`__nvr_synth=1`): usada APENAS para
  re-disparar o pipeline nativo de troca de faixa quando o CORRENTE
  muda PORQUE O MOTOR a mudou (auto-advance do mpv) — interceptada
  (nunca buscada) e NÃO embarca intenção alguma; propaga o id via
  atributo `data-nvr-sync-id` que o "Now Playing" da UI (ou o seu
  duplo de teste) relê no `loadeddata`.
- **Janela de "troca" de 700 ms por elemento**: o algoritmo de
  carga do elemento de mídia dispara `pause`/`emptied` quando o
  `src` muda; esses eventos de CARGA são distinguíveis de eventos
  de USUÁRIO pela janela, e jamais viram `/pause` fantasmas
  disputando com o `/play` que acabou de ir.

### 2. Roteador de intenções (UI → /api/v1/*)

Um único delegador em CAPTURE na `document` classifica o controle
tocado (ou o arrasto de slider) e escolhe a rota:

| gesto da UI | detecção | rota embarcada |
|---|---|---|
| tocar/pausar (botão) | evento `play`/`pause` no ELEMENTO (o player oficial chama `el.play()/el.pause()` — sinal universal, imune a renomeação de botão) | `/resume` (fallback `/play {id-atual}` se o motor está ocioso) / `/pause` |
| seek | slider de segundos (`max > 200`) OU salto programático de `currentTime` ≥ 1 s | `/seek {pos}` |
| volume | slider percentual (`max ≤ 200` ou `aria-label ~ volume`) | `/volume {level 0..100}` |
| next/prev | clique em botão (`aria-label` EN/PT/ES) | `/next`/`/prev` SE a fila do motor tem > 1 item; senão o swap da própria UI embuta `/play {id}` (fallback universal — sem fila, "next" não existe) |
| shuffle | estado do controle (`aria-pressed`/classe) | `/shuffle {value}` |
| repeat | CICLO de classe do ícone (`rep-off→rep-all→rep-one`) mapeado ao vocabulário do motor | `/repeat {off\|playlist\|track}` (alinhado aos aliases A3) |
| stop | botão | `/stop` |

Propriedades:

- **De-dupe temporal por rota (300 ms, payload igual)** — protege
  contra duplos (ex.: slider `change` + salto `currentTime`) sem
  atrasar gestos distintos.
- **Anel de últimas 32 intenções** em memória (superfície de teste
  `_t`, jamais trafega).
- **Falha de chamada**: `401/403` do gate ⇒ DEGRADE (item 3);
  `503 engine-down` ⇒ mantém ENGAGED (comandados ficam registrados
  no store, como A3/A4 definem) + dica "motor offline";
  `503 engine-idle` ⇒ descartado em silêncio (corrida benigna).

### 3. Sincronia reversa (motor → UI) + estados

Polling leve de `GET /status` (1 s visível / 5 s oculto — a decisão
de transporte do ADR-002: v0.64.2 sem canal push) projeta estado no
elemento nativo com ESCREVAS GUARDADAS (marca de sequência ignorada
pelos próprios ouvintes do bridge): posição/duração por relojinho
sombras interpolado a 1 Hz, pausa/toque em sincronia, volume,
slider de busca, e re-disparo de troca de faixa (item 1).

Estados da máquina:

- **PASSIVE** (sem sessão no browser — pré-login): inerte e INVISÍVEL
  (spec: "age só com sessão ativa"; nada decora a tela de login).
  O watcher de sessão (LS polido a 500 ms, teto 10 min) promove a
  transição no ato do login.
- **ENGAGED** (sessão presente + âncora confere): interceptação
  plena.
- **DEGRADED (razão)**: restauração completa do modo nativo
  (originais de protótipo, URL real volta no elemento, `muted`
  restaurado) + badge explicativo discreto canto inferior direito:
  - `anchor-drift` — versão da UI diferente da âncora (o gate de
    upgrade do A2 correlaciona via beacon);
  - `gate:service-account-forbidden` — alguém abriu a UI com o
    login da conta de serviço (L2);
  - `gate:session-*` — sessão morta/expirada (L1/L3) — com
    RE-PROBE periódico (30 s) que re-engaja quando a sessão
    voltar;
  - `fatal:*` — qualquer exceção interna reverte ao nativo: o
    bridge NUNCA quebra a UI oficial.
- **ENGINE-DOWN** (sub-estado do ENGAGED): sessão válida, motor
  fora — o bridge continua pilotando comandos (store) e exibe a
  dica dedicada.

Autorização: todo `/api/v1/*` sai com `Authorization: Bearer <JWT da
página>` (canal forte do gate; o probe L3 anda sobre ele) e, só no
pad de beacon (`/api/v1/spy`, cujo `sendBeacon` não monta headers), o
corpo leva o campo `jwt` — o qual o ESPÍÃO redige no chokepoint de
escritura (o gate o lê ANTES). O reenvio é para a MESMA origem que
emitiu o token: neutro por construção (mesma posição jurídica do
cookie).

## Alternativas consideradas

1. **Injetar um "player fantasma" próprio** (sombra completa do
   player, ocultando o oficial): rejeitada — viola o princípio da
   casa "o UI oficial continua funcional/estudável"; duplica toda
   a lógica do player (custo de manutenção na subida de versão);
   impossibilita o downgrade elegante ("modo nativo").
2. **Interceptor de `XMLHttpRequest/fetch` + reinjeção de DOM
   customizado** (Monkey-patching profundo do framework): rejeitada
   — alto acoplamento à estrutura interna do bundle (quebraria a cada
   upgrade do UI, justamente o cenário que o âncora administra); o
   swap na fronteira de mídia é a única fronteira ESTÁVEL entre o
   browser e o consumo de upstream (observável em todas as
   variantes do Subsonic).
3. **Só escutar eventos (click/key), sem interceptar o transporte**:
   rejeitada — sem o swap silencioso o browser CONSUMIRIA o stream
   em paralelo ao mpv (banda dupla, latência dupla, e no 2W o
   upstream seria sobrecarregado); além disso o controle de
   "corrente por motor" (auto-advance) ficaria impossível de
   seguir.
4. **WebChannel/WebSocket push próprio entre bridge e serviço**
   (novas rota de servidor): rejeitado no ADR-002 — superficie
   maior, outra porta para escalar, e a família (C1/2W) ganha
   nada; o póling de `/status` a 1 s já dá a sensação de
   resposta dentro do orçamento da caixa.

## Consequências

- Positivas: consumo de upstream zero durante uso (econômico no
  2W); UI oficial 100% funcional em todos os modos (stock,
  engaged, degradado — sempre revertível); superfície mínima de
  acoplamento (fronteira `src` + eventos DOM públicos, não
  internals); auditoria completa (espíão + anel de intenções);
  testabilidade hermética (fixtures + fake upstream + mock mpv,
  tudo já registrado na casa).
- Custos aceitos: ~1.4 kB de JS extra por página; latência de
  projeção ≤1 s (polling); o "Now Playing" de trocas por motor
  depende do DUPLO da UI ler o atributo (no SPA real, a lib de
  player atualiza por seus próprios canais — o atributo é
  redundância benéfica e não dependência).
- Resíduos documentados:
  - Um UI que migrasse para **MSE/hls.js** (`srcObject` sem `url`)
    escaparia do classificador (degradação graciosa: controle
    segue funcionando, transporte não interceptado) — o próximo
    âncora deve capturar isso no boot (a suite espiã já vigia
    `/rest/stream`).
  - Labels de botões desconhecidos (localizações futuras) caem no
    fallback por eventos do elemento (play/pause/seek) — os
    controles de LISTA (shuffle/repeat) poderiam ficar cegos em
    um markup radicalmente novo; mitigação: o ciclo por classe
    é o padrão Material do v0.64.2 e o A2 corrige divergências no
    gate de upgrade.
  - Sessão que morre DURANTE a navegação (expiração) só re-engaja
    no RE-PROBE (até 30 s) ou numa nova navegação; a janela é
    aceitável (o badge explica).

## Verificação

Matriz browser-level hermética (Chrome real, C1):
`suite/bridge_e2e.py` (T1..T15: passive/pré-login, play/pause/
resume/seek/volume/next/prev/stop/shuffle/repeat, sincronia
reversa c/ re-disparo por motor, 403 de conta-de-serviço com
restauração nativa, drift de âncora, recuperação pós-reautenticar,
varredura de invariantes de credenciais sobre TODOS os artefatos,
e a asserção central: ZERO requisições de stream do browser durante
engaged). Lint estático do asset: `tests/test_bridge_asset.py`.
Registro de evidências: `docs/reports/a6-<ts>-*/`.
