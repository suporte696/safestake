# Guia Rápido - Fase 5 (Timer e Notificações)

## Objetivo

Implementar e validar:

- lembrete de envio de resultados;
- prazo de 12h;
- bloqueio automático por não reporte;
- caixa de notificações no front.

## Passo a passo

1. Atualize o banco para incluir:
   - tabela `notifications`;
   - colunas `users.is_blocked`, `users.blocked_reason`, `users.blocked_at`.
2. Suba a aplicação:
   - `uvicorn main:app --reload`
3. Crie um cenário de teste:
   - torneio iniciado (ou já vencido) sem `match_result`.
4. Dispare jobs:
   - acessando `/dashboard` (jobs rodam automaticamente) ou
   - como admin, `POST /api/jobs/run-deadlines`.
5. Valide notificações:
   - abra o sino no header;
   - confira o contador e lista.
6. Valide bloqueio:
   - após ultrapassar 12h sem resultado, o usuário deve ficar bloqueado.
7. Valide desbloqueio por envio:
   - jogador envia resultado em `/player/results/new`;
   - bloqueio por motivo de prazo deve ser removido.

## Endpoints da fase

- `GET /api/notifications`
- `GET /api/notifications/unread-count`
- `POST /api/notifications/{notification_id}/read`
- `POST /api/jobs/run-deadlines` (admin)
