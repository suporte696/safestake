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
- [ ] Revisar todas as telas restantes para remover qualquer referência legada em BRL.

2) Saque com alerta para admin
- [x] Criar endpoint para solicitar saque com valor + chave PIX.
- [x] Notificar admins com nome, valor e PIX de destino.
- [x] Exibir solicitações recentes de saque no painel admin.
- [x] Implementar workflow completo (aprovar/reprovar saque e baixa automática de saldo).

3) Oferta do jogador
- [x] Permitir editar/corrigir oferta do jogador (quando ainda sem aportes).
- [x] Corrigir barra de progresso para refletir venda sobre a meta da oferta.
- [ ] Permitir edição com aportes já recebidos (com recálculo seguro de escrow).

4) Resultado da partida
- [x] Permitir correção do resultado antes da aprovação do admin.
- [x] Notificar admin no envio/atualização de resultado.
- [x] Notificar jogador em aprovação/reprovação.
- [ ] Implementar evidência visual no front para status de notificação por resultado.

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