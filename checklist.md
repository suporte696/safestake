Checklist priorizado (execução)

Fase 1 — Admin/KYC (prioridade máxima)
[x] Criar `routers/admin.py` com dashboard admin.
[x] Criar fluxo de revisão manual KYC (pendente/aprovado/rejeitado).
[x] Criar modelo para documentos KYC (documento + foto, status, revisor).
[x] Adicionar upload seguro de arquivos (`services/storage.py`).
[x] Bloquear operações sensíveis até KYC aprovado.
[x] Expandir e normalizar suporte de rooms SharkScope em `constants.py`.

Fase 2 — Fluxo jogador pós-partida
[x] Tela/rota para jogador enviar resultado da partida.
[x] Upload do screenshot/comprovante.
[x] Registrar valor ganho e valor enviado.
[x] Marcar tudo como pendente de revisão admin.

Fase 3 — Revisão admin e distribuição
[x] Tela admin para revisar resultado e valores.
[x] Aprovar/rejeitar resultado manualmente.
[x] Implementar distribuição financeira:
[x] apoiador proporcional.
[x] taxa da plataforma (8% sobre ganho do apoiador).
[x] restante para jogador.
[x] Registrar trilha de auditoria da distribuição.

Fase 4 — Escrow e estorno
[x] Formalizar estado de escrow por oferta/torneio.
[x] Liberar buy-in ao jogador só quando atingir integral.
[x] Estornar automaticamente se não completar / não jogar.
[x] Criar rotas de status e ações de escrow.

Fase 5 — Timer e notificações
[x] Notificação na home para lembrar envio do resultado.
[x] Job para prazo de 12h.
[x] Regra de bloqueio por não reporte.
[x] Caixa de notificações no front.

Fase 6 — Agendamento de call (simples)
[x] Modelo + rota de agendamento.
[x] Tela simples de slots/solicitação.
[x] Status: pendente, confirmado, cancelado.

Checklist — Updates e correções (PDF 06/03/2026)

1) Saldo em USD + conversão PTAX
- [x] Criar integração PTAX (BCB) para cotação USD->BRL no depósito.
- [x] Ajustar depósitos para entrada em USD e checkout em BRL (via PTAX).
- [x] Exibir valores de carteira e histórico principais em USD.
- [x] Revisar todas as telas restantes para remover qualquer referência legada em BRL.

2) Saque com alerta para admin
- [x] Criar endpoint para solicitar saque com valor + chave PIX.
- [x] Notificar admins com nome, valor e PIX de destino.
- [x] Exibir solicitações recentes de saque no painel admin.
- [x] Implementar workflow completo (aprovar/reprovar saque e baixa automática de saldo).

3) Oferta do jogador
- [x] Permitir editar/corrigir oferta do jogador (quando ainda sem aportes).
- [x] Corrigir barra de progresso para refletir venda sobre a meta da oferta.
- [x] Permitir edição com aportes já recebidos (com recálculo seguro de escrow).

4) Resultado da partida
- [x] Permitir correção do resultado antes da aprovação do admin.
- [x] Notificar admin no envio/atualização de resultado.
- [x] Notificar jogador em aprovação/reprovação.
- [x] Implementar evidência visual no front para status de notificação por resultado.

5) Transparência de apoiadores
- [x] Mostrar apoiadores e percentual na área do jogador.
- [x] Mostrar apoiadores e percentual no painel admin.

6) Escrow / saldo do jogador no aporte
- [x] Atualizar saldo do jogador no momento do aporte (saldo em jogo/escrow).
- [x] Estorno reduzindo saldo em jogo do jogador.
- [x] Automatizar transição escrow -> disponível quando jogador confirmar início da partida.
- [x] Adicionar opção explícita "não vou jogar" para disparar retorno automático.

7) Fluxo do apoiador / navegação
- [x] Revisar rotas e estado de sessão para reproduzir e corrigir bugs de "precisar deslogar".

---

## Checklist — Alterações importantes (06/03/2026)

### Perfil e contexto
- [ ] **1.** Perfil admin: permitir alternar entre visualização **Admin** ou **Jogador** para separar contextos e organização.
- [ ] **5.** Três níveis de perfil: **Admin**, **Jogador**, **Apoiador**.
- [ ] **16.** No perfil admin, não exibir "Dashboard do Apoiador".
- [ ] **33.** No perfil **jogador**, não exibir painel/dashboard do Apoiador.

### Painel admin
- [ ] **2.** Painel admin: separar contextos por **abas** (ex.: torneios, etc.) em vez de tudo vertical.
- [ ] **4.** Torneios ativos: botão **"Ver detalhes"** para ver todas as informações do torneio (admin).
- [ ] **6.** Remover "Últimas revisões".
- [ ] **32.** Revisão de ganhos (admin): exibir **qual torneio**, detalhes do torneio, **quem apoiou** (nicks e valor).

### Horário e sala
- [ ] **3.** Horário do torneio: **forçar o mesmo horário em todo o sistema** ao publicar (corrigir alteração de horário; hoje usa UTC+3 e altera ao publicar).
- [ ] **9.** Criar torneio: **obrigatório** escolher sala; hoje não aparece opção e está criando tudo como GGPoker.

### Acerto de contas e PIX
- [ ] **7.** Ícone de **copiar PIX** para o admin (na tela de solicitação de saque / acerto).
- [ ] **8.** **Controle de saque do admin**: coluna com **valor convertido em reais** (API). Não adicionar essa coluna no acerto de contas do jogador — só na tela de solicitações de saque do admin.
- [ ] **22.** Trocar texto **"VALOR A RECEBER"** para **"VALOR A SER PAGO"**.
- [ ] **23.** Máscara na chave PIX (email, CPF, telefone) no **cadastro** e no **acerto de contas**.
- [ ] **25.** Apoiador solicita saque → admin recebe em "Solicitações de saque"; **remover** coluna PIX do acerto de contas; PIX fica só na tela de solicitação de saque (admin).
- [ ] **27.** Solicitações de saque: usar **switch** aprovar/rejeitar em vez de dois botões.
- [ ] **30.** Permitir **editar valor ganho** em acerto de contas enquanto o status for **"Em revisão"**.
- [ ] **31.** Criar status **"Em revisão"** para informes de ganho (fluxo de revisão do admin).

### Saldo e pagamentos
- [ ] **24.** Adicionar **Saldo Pendente** (além de recebido, investido e carteira). Só vai para "Recebido" depois de marcar como pago; caso contrário fica pendente.
- [ ] **26.** Revisar fluxo de **marcar como pago**: saldo deve cair corretamente para o apoiador.
- [ ] **28.** Corrigir Internal Server Error ao aprovar/reprovar (debitar saldo): `FOR UPDATE cannot be applied to the nullable side of an outer join`.
- [ ] **29.** Verificar **transações reais**: valor saiu da conta do apoiador mas **não caiu no jogador** — rastrear fluxo e corrigir conciliação.
- [ ] **37.** **Carteira do jogador não encontrada** — revisar fluxo (ex.: exibição/consulta de carteira).
- [ ] **38.** Permitir **editar valor do apoio** que foi dado pelo apoiador (quando permitido pelo fluxo).
- [ ] **39.** Corrigir: após o **primeiro apoio** (se não for o valor completo disponível), o sistema **não permite mais apoiar** — liberar novos aportes quando houver saldo disponível.
- [ ] **40.** **Input de valor de investimento em Dólar** (USD) onde aplicável.

### UI e notificações
- [ ] **10.** Detalhes da stake: abaixo de "Disponível" e da porcentagem, mostrar também o **valor ainda disponível** (ex.: buy-in 200 USD, 50% → 100 USD) no mesmo card.
- [ ] **11.** Avisos/notify: exibir no **canto inferior direito** em vez do superior direito.
- [ ] **12.** Footer: **não** fixed; deve ficar lá embaixo, sem conteúdo abaixo dele.
- [ ] **34.** Tornar **todo o card da stake** clicável (não só um botão).

### Ofertas e markup
- [ ] **13.** Botão "Oferta" / "Fazer oferta personalizada": **não disponível** com mensagem **"Em breve"** (cadeado).
- [ ] **14.** Revisar fluxo do **markup**: valor total que o apoiador pode pagar deve **sempre** levar em conta o markup; acima de 100 está dando erro.

### Cadastro e stake
- [ ] **35.** Adicionar campo **plataforma** de volta no cadastro de stake.

### Visão do apoiador
- [ ] **15.** Visão Apoiador: dividir por **abas**; dar mais destaque ao **histórico de stakes**; "Minhas Propostas" com cadeado e "Em breve".
- [ ] **36.** Painel do apoiador: cards em **4 blocos** — 1. Saldo atual da carteira | 2. Saldo Pendente | 3. Total Investido | 4. Total Recebido.

### Torneios e jogador
- [ ] **17.** Jogador: **modal de detalhes do torneio** (apoiadores, iniciar etc.) e opção de informar que **vai iniciar o torneio** mesmo sem atingir a meta.
- [ ] **18.** Torneios ativos: mudar de **listagem** para **cards** (2 ou 3 colunas) com modal de detalhes; no modal: aviso **"Porcentagem não atingida, deseja iniciar a partida?"** quando aplicável.
- [ ] **19.** Botão **"Encerrar torneio"**: liberado só com **meta total** atingida **ou** quando o jogador informar que vai jogar (independente da porcentagem).
- [ ] **20.** Notificação no painel do jogador para **informar ganhos**: temporizador de **24 horas**; se não informar, **conta suspensa** até que informe (partidas em aberto/aguardando resultado).
- [ ] **21.** **Linkar** acerto de contas ao modal de cada torneio.

### Depósito e erros
- [ ] **41.** Corrigir erro no depósito: **"Não conseguimos iniciar seu depósito agora. Tente novamente em instantes."** — investigar e tratar causa.