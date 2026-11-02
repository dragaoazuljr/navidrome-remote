# ADR-007 — Gate de compatibilidade por versão: identificação POSITIVA por feature-probing

**Data:** 2026-11-01
**Autor:** Dev (lane A9, t_6c85c543)
**Status:** DECIDIDO (implantado no bridge `0.5.0-a9`; incorporado ao gate A2 como etapa G6)
**Estende:** cláusula ANCHOR-DRIFT do asset A8 (t_2b919598/03aebff); ADR-002 (fallback registrado); spec signoff 2026-10-24 (UI atualizável; âncora v0.64.2; Fallback 1 cirúrgico só então)

## Contexto

O gate vigente do asset confiava exclusivamente em uma ETIQUETA: o campo
`__APP_CONFIG__.version` publicado pela SPA. Duas classes de mudança
ficavam cegas para ele:

1. **Hook partido (broken hook).** Um upgrade que renomeie/remova a
   chave `version` (refatoria banal de contrato) deixa a declaração
   `null`/ausente. Pela regra A1 ("desconhecido não rompe"), o bridge
   seguia SEM classificar — exatamente o inverso do que se quer quando o
   contrato quebrou: trataríamos uma geração estranha como confiável e
   interceptaríamos um player cujo JSX/canais já mudaram.
2. **Drift de rotas.** Mesmo com `version` intacta, a geração seguinte
   pode mudar o canal de entrega (ex. `/rest/stream2`). Um bridge
   ancorado só reconhece o RX oficial da ANCORA; um canal estranho
   chegando num elemento de mídia DURANTE engajamento indica o hook
   podrendo — sem braço de detecção, isso passa despercebido até quebrar
   o usuário.

O spec assinado (2026-10-24) é claro: a UI oficial permanece
**atualizável**; o fallback (ajuste cirúrgico ancorado em v0.64.2) vale
para o caso em que o UPSTREAM ROMPER a injeção — e a única forma honesta
de saber disso é uma classificação POSITIVA por features, não por
etiqueta.

## Decisão

1. **Feature-probing positivo.** O bridge classifica a geração em
   `known` / `drift` / `unknown`:
   - CONTRATO EMPÍRICO da geração ancorada (caracterizado em 2026-11-01
     contra o v0.64.2 AO VIVO no LXC 9102): `window.__APP_CONFIG__` NÃO
     existe no HTML bruto — a SPA a instala EM RUNTIME; o carrier é uma
     STRING SERIALIZADA com campo `version` (a forma objeto também
     conta, por tolerância frontal); o valor publicado é o `serverVersion`
     COMPLETO (ex. `"0.64.2 (10114574)"`), então toda comparação
     normaliza a BASE primeiro (descarta prefixo vendor `v`, e tudo
     após `(` ou `-`);
   - `drift` — a BASE NORMALIZADA da versão declarada difere da base da
     âncora (ordem contratual: a declaração decide ANTES do probe);
   - `unknown` — contrato NÃO verificável (chave remanejada/ausente,
     objeto morto) — **nunca interceptar**: UI 100% nativa, `console.warn`
     visível em devtools (aceite: "aviso"), beacon `feature-drift`
     (auditoria/espião) e badge. Não é "curado" pelo re-probe de 30 s:
     features são estáveis dentro da vida da página.
2. **Braço tardio (demolição preguiçosa).** Durante engajamento, um canal
   de mídia que NÃO casa com o RX oficial da âncora (nem `data:`/`blob:`
   nem feed sintético) caindo num elemento do player → demolição
   assíncrona UMA vez por engajement: o hook podreu sob nossos pés
   (mudança de versão no meio da vida) → devolver UI nativa + aviso.
3. **Correlação no gate (G6).** A suíte A2 ganha a etapa `ui_fingerprint`
   (G6): roda o PRÓPRIO probe do bridge (`_t._classify()`) num chrome
   real sobre o ORIGIN PUBLICADO do listener (onde a SPA instala o
   carrier em runtime) e correlaciona a declaração com o canal
   black-box independente do G0 (`/rest/ping`; o HTML estático não
   publica o config — informativo). Veredito `green`/`red`;
   **RED (inclusive contradição bridge×black-box) impede o
   green-close** do spike — o caminho registrado (ADR-002: ajuste
   cirúrgico ancorado; ou re-ancoragem registrada) vira obrigatório.
   `--expect-version V` permite declarar a versão esperada da rodada
   (match beyond-the-anchor). `ui_class` entra nos `_key_metrics` e no
   diff G5 (mudança de classe = MAJOR; nova geração = sinal de
   re-spike). Sem chrome → `pendente-QA` (neutro: nem bloqueia nem
   aprova — mesma regra do G4 B(ii)).
4. **Teto do asset: 91k → 96k.** Precedente: ADR-006 elevou 90k→91k
   quando o asset lacrado do A8 (90.107 chars) estourou o teto anterior
   na própria baseline. A9 é a próxima CAMADA de defesa (~+3,2 kB de
   código de runtime além da documentação), landing 94.682 chars; teto
   96.000 mantém o tripwire em escala de camada (~1,4 %, idêntico ao
   folguem do ADR-006: ~1,0 %). Regra continuada: o teto acompanha camadas
   implementadas e QUALQUIER crescimento futuro exige decisão registrada
   primeiro (nunca ajuste tácito do lint).

## Consequências

- **Aceite A9 coberto por três níveis:** unidade (matriz de decisão +
  cross-lock do RX + contrato dos fixtures), E2E **T19** (fixture
  `broke.html`: hook partido → warning no console + `hook-mismatch` +
  beacon `feature-drift` + zero tráfico API + player nativo tocando no
  canal novo via `/rest/stream2`) e gate vivo (G6 no LXC 9102).
- Toda rodada futura de upgrade roda o gate; um `G6 RED` bloqueia o
  green-close até (a) novo ADR registrando re-ancoragem ou (b) ajuste
  cirúrgico no tag antigo (Fallback 1) — o spec "UI atualizável" é
  protegido sem jamais interceptar uma geração que não comprovamos.
- O espionagem não muda de forma: `feature-drift` cavalga o pad
  `/api/v1/spy` existente (o servidor já redacta; invariantes de
  segredo intactas — o detail nunca carrega u/t/s).

## Contrato empírico (registro A9, 2026-11-01)

Medição direta no LXC 9102 (v0.64.2, build 10114574): HTML raiz SEM
`__APP_CONFIG__`; carrier instalado em runtime como string com campo
`version` carregando o `serverVersion` completo; G6 mediu `known`
(comportamento do probe acima reproduzido por G0/G6 e pelos fixtures
`main`/`drift`/`broke`). Qualquer alteração futura deste contrato é,
por definição, o evento que o gate existe para detectar.

## Alternativas consideradas (rejeitadas)

- **Manter o gate de etiqueta (status quo):** rejeitado — cego contra
  quebras de contrato com `version` ausente; viola o espírito do "nunca
  romper o player".
- **Pin rígido: só aceitar `0.64.2`:** rejeitado — mata upgrades
  legítimos que mantenham o contrato (o spec assinado diz que a UI é
  atualizável; o gate não é o muro).
- **Probe profundo (selectors/DOM internos da SPA):** rejeitado — acopla
  o bridge a minúcias que mudam a cada sprint; sonha só com o contrato
  PÚBlico (objeto de config + canal de mídia), o mínimo que caracteriza
  a geração.
