# ADR-003 — Controlador mpv: driver JSON-IPC, reconciliação por id, gapless best-effort

Data: 2026-10-28 · Ciclo: A4 · Decisor: Dev (implanta), ratificado em review (Tech-lead)

## Contexto

A3 entregou `/api/v1/*` + store de intenção persistente, mas o transporte
era executado peça por peça pelos handlers da API e o estado vivia em dois
mundos sem ponte: a fila do cliente (ids Navidrome) nunca chegava ao
playlist do engine identificável, e o que o engine tocava nunca voltava
para a fila. A4 fecha essa lacuna introduzindo UMA camada —
`navidrome_remote/controller.py::PlaybackController` — entre a API
(presentação) e o `MpvEngine` (transporte), com três responsabilidades:

1. **QUITA**: empurrar a fila Navidrome para o playlist do engine,
   entrada por entrada, TAGGEANDO CADA ENTRADA com o id da música
   (`playlist-add <url> <i> <id>`) — assim o engine responde
   "o que está tocando?" (`playlist-current-id`) e a resposta mapeia de
   forma inequívoca de volta para a fila.
2. **TRANSPORTE**: next/prev/seek/stop/pause/volume/shuffle/repeat como
   primitivas STOCK do mpv (um comando IPC cada; tabela de tradução no
   docstring do `api.py`).
3. **ESTADO**: `snapshot()` lê o conjunto de observação em UM round
   batched e `reconcile()` faz `store.index` seguir o ENGENHE enquanto
   há faixa ativa (ancorado no `playlist-current-id` — imune a
   reordenações de shuffle), persistindo concordâncias via
   `mark_selected(persist=True)`. Quando nada toca, a fila segue segurando
   a intenção (semântica A3; retomada pós-reboot intacta).

## Decisão

- O protocolo de aplicação da fila muda de `loadfile <u0> "replace"` +
  bulk-append para `playlist-clear` → `playlist-add <url> <i> <id>` (por
  entrada) → `playlist-play <índice persistido>` → espelho de configurações
  (volume/shuffle/loops). Motivo: a primeira forma deixava a entrada
  inicial SEM id (loadfile substituída), rompendo a reconciliação na faixa
  1; a nova dá id a TODAS as entradas, incluindo a que começa a tocar.
- `seek` sai como `seek <t> "absolute"` com clamp à duração carregada (o
  engine reporta `duration`; buscar além do EOF só assenta no fim — sem
  avanço spurious).
- Shuffle segue a propriedade stock `shuffle`: a escrita 0→1 faz o engine
  reordenar seu próprio playlist (entrada corrente no lugar); 1→0 apenas
  limpa a flag (o engine restaura o layout).
- Repete: pares `loop-file`/`loop-playlist` conforme tabela única
  `controller.REPEAT_PLAN` (alias `one`→track, `all`→playlist, spelling
  da UI Navidrome).
- **Gapless BEST-EFFORT** (qualidade-alvo do 2W; medido, nunca prometido):
  * a fila INTEIRA é carregada no apply — o demuxer do engine faz
    prefetch da próxima entrada (nenhum loadfile storm no meio da
    playlist, nenhuma janela ociosa entre faixas);
  * transições de transporte usam `playlist-next`/`playlist-prev` (a
    troca NATIVA dentro do pipeline de decodificação do mpv) em vez de
    unload/reload;
  * a unidade de engine fixa `--gapless=yes` (padrão do mpv, tornado
    explícito em `deploy/navidrome-remote-mpv.service`);
  * silêncio RESIDUAL entre formatos (sample rate/layout) é propriedade
    DOS ARQUIVOS, limitada pelo mecanismo de gapless do próprio mpv; o
    resíduo é MEDIDO em C1/C3 pelo espaçamento temporal entre eventos
    consecutivos `file-loaded` (o mock v0.3 emite `t_monotonic` por
    evento exatamente para isso), não escondido nem negado.

## Alternativas consideradas

- **Reconciliar por `media-title`**: descartado — título sintético do
  `/rest/stream` (basename vazio) colide; o id é injeção nossa,
  incolidente por construção.
- **Manter `loadfile <u0> replace` + adicionar o resto**: rejeitado —
  perde o id na primeira entrada (exatamente a faixa que inicia a
  sessão de retomada).
- **Gapless garantido com buffer duplo em Python**: fora de escopo do
  control plane — o engine já faz; reinventar aqui criaria duas fontes
  de verdade para o mesmo áudio.

## Compatibilidade de versão (nota de risco)

`playlist-add <uri> <index> <id>` exige suporte ao parâmetro `id` no
motivo de destino. Builds antigos (< ~0.35) recusariam a forma 3-arg; o
fallback registrado seria `playlist-add <uri> <index>` + reconciliação
por posição (`playlist-index`) mantida pelo controller. O alvo de
produção (2W) e o mock C1 suportam a forma completa; se o motivo do 2W
prevenir a terceira forma, o controle cai para o fallback posicional
SEM regressão de contrato da API (o campo observado
`playlist-current-id` simplesmente deixa de existir e a reconciliação
abstém). Nenhum contrato do M1 muda nesse cenário.

## Consequências

- Camada `controller.py` bloqueante (drive do MpvEngine); callers async
  envolvem com `asyncio.to_thread` (a API o faz).
- Aditivos no `GET /status`: `play.dur` e `queue.size` (contagem do
  ENGINE, permitindo ao cliente comparar intenção × engine em vista).
  Contract M1 conservado por-key.
- Mock `tools/fake_mpv_sink.py` v0.3: playlist-aware com id, shuffle
  write-triggered com restore, avanço natural de EOF respeitando
  loop-file/loop-playlist, seek com unidades, `t_monotonic` nos eventos
  (medição gapless). Em 2W/C3 o programa é trocado 1:1 por
  `mpv --idle=yes --gapless=yes --input-ipc-server=<mesmo socket>`.

## Status

Aceite (card A4): driver pilota mpv (quota/next/prev/seek/shuffle/repeat/
gapless-best-effort) ✔ · estados UI↔engine consistentes via status ✔ ·
tradução /api/v1 → IPC centralizada e verificada pela suíte C1
(movido do handler para o controller; regressões A2/A3 intactas).
