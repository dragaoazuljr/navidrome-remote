# Proposta de replanejamento: escada de testes dev-workspace → Pi Zero 2W → Orange Pi Zero 2W

- **Data:** 2026-10-27
- **Autor:** Tech-lead (perfil Hermes)
- **Destinatário:** PO (alinhamento de tarefas no board)
- **Status:** proposta — não altera a spec até ratificada
- **Referências:** KB `scopes/personal/projects/navidrome-remote.md` (spec v1.1), `media-stack.md`, cláusulas A/B do pacote aprovado em 2026-10-24

## 1. Objetivo

Propor como o **teste no Pi Zero 2W** (caixa disponível, com 6 câmeras RTSP
operacionais) se encaixa na linha do tempo de desenvolvimento, e redistribuir
as fases para que (a) o desenvolvimento não fique paralisado aguardando
hardware, (b) a sessão final no Orange Pi Zero 2W seja enxuta, e (c) o gate
de smoke do M0 não concentre risco arquitetural e risco de hardware na mesma
sessão.

## 2. Baseline: o que o planejado atual diz (revisão do histórico)

Histórico registrado no KB:
1. **2026-10-22** — Spec v1.0: varianta Android (Galaxy A20 como servidor HTTP +
   player Kotlin/Ktor/Media3) no media-stack como SPEC-001. Viabilidade passou
   (CORS `*`, motor MIT), estimativa 5–9 semanas.
2. **Pivô** — o executor mudou para um mini-computador fixo: eliminou o
   tributo de plataforma (FGS, políticas de bateria/OEM, toolchain Android) e
   reduziu a estimativa para ~2–3 semanas.
3. **2026-10-24** — Spec v1.1: **SBC Orange Pi Zero 2W (1GB)** + DAC USB-C →
   3,5mm → Harman Kardon Go+ Play 3. Pacote aprovado com o arquiteto
   (cláusulas A/B; rota P1 = LAN crua `.0/24` ao LXC 9102; serviço único
   proxy+API+bridge; gate de sessão; invariante de credenciais; âncora
   v0.64.2).
4. **Pipeline atual (KB):** (1) viabilidade ✅, (2) documentação ✅,
   (3) arquiteto ✅, **(4) planejamento com PO ⏳**, (5) dev/testes ⏸
   *"gate: chegada de SBC + DAC"*, (6) implementação ⏸.

Milestones atuais (todos *após* o hardware):
- **M0 – Spike** (½–1 dia): montar o 2W; serviço único + script espião
  (rotas chamadas no boot do player v0.64.2, comportamento OPTIONS/CORS, se o
  player emite Upgrade/WS); mpv manual no DAC. O **aceite mistura arquitetura
  (verdes da cláusula B, medição por rota) com hardware (música saindo na
  caixa)**.
- **M1 – MVP** (1–2 semanas part-time): `/api/v1/*` com token + gate de
  sessão; `bridge.js` definitivo; fila/estado persistentes.
- **M2 – Endurecimento** (1–2 fins de semana): watchdog, política de desgaste
  de microSD, fallback BT, logs.

Problemas abertos registrados: desgaste do SD; 1GB de RAM (zram condicionada
à medição do M0); alimentação do DAC (≤500mA); maturidade da imagem
community do 2W; spike aguardando hardware.

**Diagnóstico do baseline:** o gate do M0 ficou amarrado à chegada do 2W
porque era a única plataforma "real" disponível. Consequências:
(i) o desenvolvimento trava durante a logística do SBC, e (ii) a sessão do
M0 terminaria depurando transparência de protocolo numa placa de 1GB pouco
familiar, coabitando câmeras — loop longo e ruidoso. A objeção antiga ao
dev-workspace ("sondas do dev-ws não mapeiam a rota") está **descartada**:
desde 2026-10-27 a rota foi medida aberta diretamente (§3).

## 3. Fatos verificados (2026-10-27)

Medidos hoje, com método registrado:

1. **Rota dev-workspace → LXC 9102: ABERTA.** De `192.168.0.19` (segmento
   `.0/24`, mesmíssimo segmento da cláusula A) `TCP 192.168.0.200:4533`
   responde `302` (raiz SPA do Navidrome v0.64.2). O dev-workspace **simula a
   condição de rede da cláusula A** — melhora sobre a nota de diagnóstico do
   KB de 2026-10-24.
2. **Rota do Pi Zero 2W → Navidrome: via tailnet, funcionando.** O pi mora em
   `192.168.1.14` (Wi-Fi, segmento `.1/24`) + `100.80.125.26` (Tailscale,
   autenticado, 6 peers ativos). O host DNS `navidrome` (registrado no
   Pi-hole) resolve para o ts-gw (`192.168.0.50`); pela face tailnet o mesmo
   gateway está em **`100.90.124.50:80`** (nginx): `GET /` → `302`
   (`Location: http://navidrome.lan/login?...`) → `GET /login` → **`200`,
   HTML da SPA**. As portas 4533/443 do ts-gw estão fechadas na face tailnet:
   o caminho funcional é `:80` + `Host: navidrome.lan` (o "redirecionamento
   porta 4533" ocorre internamente no gateway → LXC). `OPTIONS` sem sessão na
   rota REST: `401` sem headers CORS (o gateway não sintetiza).
   **Conclusão prática:** para o SBC, o upstream do proxy é o ts-gw via
   tailnet (ou LAN crua quando o segmento permitir) — **variante P2 sem
   daemons novos**; a cláusula A passa a considerar a fronteira
   "sessão Navidrome + o que o gateway expõe", e a escolha da rota final
   depende do segmento onde a Go+ estará (§7, decisão D1).
3. **Perfil do Pi Zero 2W (candidato a cama de smoke):** aarch64, 4× A53
   (mesma família de núcleo do H616 do 2W — **extrapolação CPU válida**),
   uptime 10 dias, load 0.00; porém **RAM efetiva 415Mi** (firmware antigo
   reporta bem abaixo do 1GB; o 2W recém-imagemado deve reportar mais),
   ~98Mi livres, sem swap; rodando **LightDM (desktop) + pcmanfm, mediamtx
   (6 câmeras RTSP, :8554/:8892) e Tailscaled** (~60–100Mi do total).
   Áudio: sai pela **3,5mm onboard** (`card0 vc4-hdmi`, `pcmC0D0p`) — permite
   prova auditiva sem DAC. SSH: user `pi`, chave no dev-workspace, sudo sem
   senha. A box fica no segmento `.1/24` — **não alcança o LXC por LAN crua**;
   o caminho é o ts-gw acima.
4. **Dev-workspace como plataforma de teste contínua:** x86_64, Python
   3.13.5, rede no `.0/24`. **Sem mpv instalado** (instalável) e **sem áudio
   utilizável** (VM headless) → serve para toda a camada de protocolo
   (proxy, injeção, API, gate, spy, JSON-IPC contra sink falso), **não**
   para a perna de áudio.
5. **Infraestrutura de entrega (produtos) operante:** repo
   `github.com/dragaoazuljr/navidrome-remote` criado e público; fluxo
   hermes-broker exercitado de ponta a ponta — `workspace-claim` →
   worktree isolada `/opt/hermes-workspaces/<task-id>` (branch
   `hermes/default/<task-id>`, lane `supervised`) → edição/commit →
   `workspace-request-publish` (PR para revisão) → `workspace-finish`/`cancel`.
   **Restrição operacional relevante:** lease de **4h** por claim —
   desenvolvimento multi-dia opera no ritmo "claim por sessão, PR por
   incremento" (implantação do M1 em dias separados ⇒ vários ciclos, cada um
   com PR rastreável).

## 4. Lacunas identificadas no planejamento atual

- **G1 — Gate único sobre hardware desconhecido:** M0 concentrava protótipo
  de arquitetura + validação de hardware numa sessão única no 2W (imagem
  nova, DAC, 1GB, SD, 24/7) — alto custo de depuração e sem espaço para
  iterar sem refazer a montagem.
- **G2 — Espera inútil:** itens do M0 que não dependem de hardware (verdes
  da cláusula B, mapa OPTIONS/CORS, injeção, espião do player) podiam — e
  podem — sair da frente, tirando ½–1 dia de risco crítico do gate do
  hardware.
- **G3 — Camas de teste sem hierarquia definida:** não está dito onde cada
  risco se valida; isso produz "testamos tudo em todo lugar" ou lacunas.
- **G4 — Ritmo do broker não contemplado:** M1 (1–2 semanas part-time) nunca
  coube numa lease de 4h; o plano precisa assumir o ritmo de sessões com PR
  incremental (a lane `supervised` já exige revisão humana por merge).
- **G5 — Segmento da Go+ indefinido:** a cláusula A/P1 presume `.0/24`;
  se a Go+ vive no segmento `.1/24` (como o pi), a rota do SBC final muda de
  P1 para P2 — decisão pendente que afeta a spec, não só o smoke.

## 5. Proposta central: escada de três camas de teste

Cada risco se valida na **menor cama capaz de validá-lo**; camas superiores
refazem apenas o que lhes é específico (regressão leve, não smoke completo).

| Cama | Plataforma | Valida | Não valida | Momento |
|---|---|---|---|---|
| **C1 — Contínua** | dev-workspace (x86_64, `.0/24`) | Proxy transparente (200/302, `Set-Cookie`, rewrite de `Location`), upgrade/WS, mapa OPTIONS/CORS por rota (cláusula A), injeção `bridge.js` no HTML v0.64.2, `/api/v1/*` + token + gate de sessão, JSON-IPC mpv (contra sink falso/`--null_audio`), espião NDJSON | Perda de áudio real, pressão 1GB, BT/ALSA reais, SD 24/7 | Em **todo commit/feature** do M0' e M1 (CI-local por sessão) |
| **C2 — Smoke de plataforma** | Pi Zero 2W (aarch64, 3,5mm onboard) | Boot do serviço em ARM enxuta, sessão de navegador real, transporte ponta-a-ponta → **áudio na 3,5mm**, pressão de RAM (entrada p/ zram), enumeração BT (registro, sem gate) | DAC dedicado, consumo 500mA, política SD, estabilidade 24/7 | **1 janela (¼–½ dia) após o M1**, pré-implementação final |
| **C3 — Final** | Orange Pi Zero 2W + DAC | Aceite completo da spec: áudio **somente** via Go+ (DAC primário, BT fallback), auto-start pós-reboot, zram/SD por medição, regressão do suíte C1 | — | Na chegada do 2W (implementação + M2)

Regras da escada:
- **R1:** um defeito descoberto em Cn volta a ser coberto por um caso no
  suíte C1 (regressão), a menos que seja inerente à cama (áudio/DAC).
- **R2:** C2 não é gate de *arquitetura* — vermelhos de protocolo em C2
  indicam incompatibilidade de plataforma e vão a decisão (fix, fallback 1
  ou reassessment), não a retrabalho de M0.
- **R3:** C3 aplica a **Definição de Pronto** original (5 itens) da spec;
  nada dela é movido para cima — só o *momento* das validações muda.

## 6. Remapeamento de milestones

Letters preservadas; conteúdo ajustado. **Nada do "o quê" da spec muda — muda
o "onde/quando".**

- **M0′ — Spike de protocolo (C1, dev-workspace) — ½–1 dia — começa na
  libertação pelo PO, sem hardware.**
  Conteúdo: esqueleto do serviço único (proxy + injeção + `/api/v1/*` stub +
  espião) e suíte C1 inicial. Aceite:
  1. verde-cláusula-B-(i): forward de `Upgrade`/WS sem ruptura (ou RED
     documentado com evidência do espião: o player v0.64.2 pode simplesmente
     não usar WS — dado novo para a spec);
  2. verde-cláusula-B-(ii): `Set-Cookie` do Navidrome sobrevive a
     reload/navegação no origin do SBC;
  3. mapa OPTIONS/CORS por rota (cláusula A) medido a partir do `.0/24`;
  4. injeção do `bridge.js` presente e carregando no HTML da SPA;
  5. espião registrando o boot do player (rotas, transporte, cookies);
  6. JSON-IPC respondendo comandos contra sink falso (`--null-audio`/mock).
  Saída: base de M1 com risco arquitetural **fechado antes do hardware**.
- **M1 — MVP (C1 contínua; 1–2 semanas part-time) — sem mudança de escopo**
  (transporte completo no `/api/v1/*`, `bridge.js` definitivo, fila/estado
  persistentes, gate de sessão). Executado no ritmo de **sessões de 4h**
  (claim→PR→finish), cada PR revisável pela lane `supervised`. Regras R1/R2
  aplicam; o suíte C1 cresce com cada feature.
- **M1.V — Validação em Pi Zero 2W (C2; 1 janela ¼–½ dia) — NOVO.**
  Conteúdo: implantar o M1 terminado no pi (venv + 2 unidades systemd +
  `EnvironmentFile`); janela acordada (DE pausado por decisão D2, câmeras
  intactas, Tailscale intacto). Aceite reduzido:
  1. serviço sobe no boot (systemd, `Restart=always`);
  2. navegador real → sessão ativa → play/pause/next → **áudio audível na
     3,5mm** (prova de cadeia, não de qualidade final);
  3. medição de pressão de RAM (gráfico/free antes-depoés) → insumo da
     decisão zram (vale para 2W, mesmo teto de 1GB);
  4. enumeração BlueZ registrada (informativo — BT4.2 ≠ BT5.0, **não** é
     validação do fallback);
  5. rollback limpo executado (desinstala serviços, restaura DE) — a box
     volta ao estado câmera-intacta.
  Fora do M1.V: DAC, SD, 24/7, BT como gate.
- **M2 — 2W endurecido (C3) — na chegada do 2W + DAC — absorve o antigo M2
  + a cauda do antigo M0.** Conteúdo: montagem (imagem, IP fixo), áudio
  primário pelo DAC (validar consumo ≤500mA na USB2 do 2W), BT 5.0 A2DP
  como fallback real, zram/política SD por medições do M1.V, watchdog, logs
  enxutos, **regressão da suíte C1 em C3**. Estimativa do antigo M2 (1–2
  fins de semana) **absorve o M0 original sem estouro**, porque a parte de
  arquitetura já saiu no M0′.

**Efeito no caminho crítico:**
- Hoje: `chegada 2W → M0 (½–1d) → M1 (1–2s) → M2 (1–2 fds)` = 2–3 semanas
  *após o hardware*, com desenvolvimento paralisado durante a logística.
- Proposto: `PO libera → M0′ (½–1d) → M1 (1–2s, paralelas à espera do SBC)
  → [2W chega] → M1.V (¼–½d, pode antecipar para logo após M1) → M2
  (1–2 fds)` ⇒ o hardware encontra o produto **praticamente pronto**; a
  sessão de implementação cai de "primeiro contato com o risco" para
  "montagem + validações de plataforma".

## 7. Decisões em aberto (precisam de resposta no alinhamento)

- **D1 — Segmento da Go+ Play 3 (`.0/24` ou `.1/24`)?** Define a rota final do
  SBC: P1 (LAN crua `.200:4533`) mantém-se se o SBC cair no `.0`; caso `.1`,
  a rota é P2 (ts-gw via tailnet `:80` + vhost) e a cláusula A é reformulada
  para "fronteira = sessão Navidrome + superfície exposta pelo gateway".
  Afeta: spec v1.2, arquitetura diagramada, e o upstream configurável do
  proxy (o código já nasce configurável para os dois).
- **D2 — Janela no Pi Zero 2W:** autorizar (a) pausa do LightDM e (b)
  habilitação de `zram` durante o M1.V? As 6 câmeras e o Tailscale permanecem
  intocados em qualquer cenário; rollback scriptizado no fim.
- **D3 — Mapeamento no board:** o PO adota os IDs/escopos propostos em §8
  (ou os renomeia) e define a ordem de libertação (sequencial recomendado:
  NVR-1 → NVR-2 → NVR-3 → NVR-4, com NVR-3 liberável assim que NVR-2 fecha).
- **D4 — Registro no KB:** após ratificação, atualizo a página
  `navidrome-remote.md` para spec v1.2 (escada, rota, milestones remapa
  pados, linha `resource:` com o repo) e deixo a entrada em `kb/log.md`.
  (Item de execução minha, incluído aqui para ficar registrado no alinhamento.)

## 8. Tarefas propostas para o board (PO cria/edita; eu executo)

Convention: IDs `NVR-n`; todo item executa no fluxo `hermes-broker`
(claim por sessão → PR por incremento → merge por você, lane `supervised`).
Credenciais de serviço do Navidrome entram via `EnvironmentFile` fora do
repo (invariante de credenciais).

- **NVR-1 — M0′: spike de protocolo no dev-workspace** *(½–1 dia)*
  Descrição: serviço único esquelético (proxy transparente + injeção
  `bridge.js` + `/api/v1/*` stub + espião NDJSON + cliente JSON-IPC) e suíte
  C1 com os 6 critérios de aceite do §6.
  Depende: nada (claim/fluxo já validado 2026-10-27).
  Entregas: PR(s) no repo; suíte C1 executável; relatório dos verdes/reds da
  cláusula B e do mapa OPTIONS/CORS.
- **NVR-2 — M1: MVP** *(1–2 semanas part-time; N PRs incrementais)*
  Descrição: transporte completo (`play/pause/next/prev/seek/shuffle/repeat/
  volume/queue/status`), `bridge.js` definitivo, fila/estado persistentes,
  gate de sessão; empurrão da URL Subsonic resolvido no servidor (o
  navegador nunca vê tokens — invariante).
  Depende: NVR-1. Cada PR traz seus casos no suíte C1.
- **NVR-3 — M1.V: validação em Pi Zero 2W** *(¼–½ dia, 1 janela)*
  Descrição: implantar M1 no pi (venv, 2 unidades systemd, `EnvironmentFile`
  com upstream tailnet-ts-gw); janela de D2; aceite do §6-M1.V; rollback ao
  final.
  Depende: NVR-2 (código), D1 (rota/upstream), D2 (autorizações).
- **NVR-4 — M2: implantação final no Orange Pi Zero 2W + DAC** *(1–2 fins de
  semana; inicia na chegada do hardware)*
  Descrição: montagem (imagem 64-bit, IP fixo), serviço único + mpv, DAC
  USB-C (consumo ≤500mA), 3,5mm → Go+ (primário), BT 5.0 A2DP (fallback),
  zram/SD por medição de NVR-3, watchdog, logs enxutos, auto-start
  pós-reboot; regressão do suíte C1 em C3; Definição de Pronto (5 itens)
  checklist final.
  Depende: NVR-3 + chegada de 2W e DAC (pareados).

Fora do board (execução do Tech-lead, comunicada neste doc): atualização da
spec v1.2 no KB (D4) e manutenção da paginação do pipeline (item 4
conclui com este alinhamento; itens 5/6 passam a referenciar os NVRs).

## 9. Riscos e mitigações

| Risco | Mitigação |
|---|---|
| Lease 4h < duração das fases | Ritmo "claim por sessão, PR por incremento" (explícito no §6); estado nunca vive só no filesystem da lease (commit cedo e sempre) |
| Pi = box de câmaras (disrupção real se algo quebra na janela) | Janela acordada e curta; Tailscale/câmeras intocados; DE pausado (não o mediamtx); rollback scriptizado e testado antes da janela

| RAM 415Mi no pi insuficiente p/ stack + DE | Pausa do DE (~60Mi) libera margem; se ainda apertar, `zram` (D2); último recurso: reduzir resoluções do mediamtx (só com D2 ampliada) |
| Firmware do pi sub-reporta RAM (415Mi ≠ 1GB) | Não usar números absolutos do pi como gate; usar **tendência de pressão** (antes/depois, curva) — o 2W recém-imagemado dará número fiel no M2 |
| BT do pi (4.2) gera falsos negativos p/ fallback | BT fora dos gates de M1.V (informativo) — a validação real só no 2W (BT 5.0) |
| Rota final mudar (D1) após M1 | Upstream do proxy é `EnvironmentFile` (1 linha por cenário: ts-gw `:80`+vhost ou `.200:4533`); suíte C1 parametrizada por upstream — troca sem código |
| Gateway ts-gw evolui/falha | O proxy é transparente: se o gateway sumisse, a rota `.0/24` crua segue como fallback configurável (D1) |

## 10. Próximos passos (assim que o alinhamento confirmar)

1. (Tech-lead) registro do acordo no KB: spec v1.2 + `kb/log.md` (D4).
2. (PO) criação/edição das tasks `NVR-1..4` com a ordem de libertação (D3).
3. (Tech-lead) `workspace-claim` do `NVR-1` no mesmo dia; primeira entrega
   esperada: PR com o esqueleto + suíte C1 + relatório cláusula B.
4. (Paralelo, sem gate) user: confirmar segmento da Go+ (D1) e dar luz
   verde/ajuste nas autorizações de D2.

_Fim da proposta._
