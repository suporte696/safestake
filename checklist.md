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