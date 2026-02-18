# Guia de Execução - Fases 1 a 6

Este documento descreve tudo que precisa ser feito para subir e validar as funcionalidades das Fases 1, 2, 3, 4, 5 e 6 no projeto.

## 1) Pré-requisitos

- Python 3.11+ instalado
- Banco configurado em `DATABASE_URL`
- Dependências instaladas no ambiente virtual
- Permissão de escrita no diretório `static/uploads/`

Comandos sugeridos:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install fastapi uvicorn sqlalchemy python-dotenv passlib[bcrypt] jinja2 python-multipart requests
```

Se usar `requirements.txt`, prefira:

```bash
pip install -r requirements.txt
```

## 2) Atualização de Banco (obrigatório)

As Fases 1-6 adicionaram novas tabelas e colunas:

- Tabelas novas:
  - `user_documents`
  - `prize_distributions`
  - `tournament_escrows`
  - `notifications`
  - `call_schedules`
- Campos novos:
  - `users.is_blocked`
  - `users.blocked_reason`
  - `users.blocked_at`
  - `stake_offers.escrow_status`
  - `match_results.valor_enviado`
  - `match_results.comprovante_url`
  - `match_results.review_status`
  - `match_results.rejection_reason`
  - `match_results.reviewed_by`
  - `match_results.reviewed_at`
  - `match_results.submitted_at` (já usado)

### Opção A (desenvolvimento, mais simples): reset total

Use apenas se puder perder dados locais:

```bash
python3 seed.py
```

Esse comando recria o schema via `drop_all/create_all` e popula dados de teste.

### Opção B (manter dados): criar migração manual/SQL

Se não puder resetar o banco, aplique migração SQL equivalente no seu banco para as tabelas/colunas acima.

## 3) Subir a aplicação

```bash
uvicorn main:app --reload
```

Acesse:

- Marketplace: `http://localhost:8000/`
- Login: `http://localhost:8000/login`
- Admin KYC: `http://localhost:8000/admin/dashboard`
- Admin Resultados: `http://localhost:8000/admin/results`

## 4) Fluxo de validação por fase

## Fase 1 - Admin/KYC

### O que foi implementado

- Registro com upload obrigatório de documento + selfie
- Painel admin para aprovar/rejeitar KYC
- Bloqueio de ações sensíveis sem KYC aprovado

### Teste rápido

1. Cadastre um jogador e um apoiador (com arquivos no registro).
2. Tente criar oferta/investir antes da aprovação KYC (deve bloquear).
3. Faça login como admin.
4. Acesse `/admin/dashboard`.
5. Aprove KYC dos usuários.
6. Repita criação de oferta/investimento (deve permitir).

## Fase 2 - Envio de resultado do jogador

### O que foi implementado

- Tela: `/player/results/new`
- Envio de posição, valor prêmio, valor enviado
- Upload de screenshot e comprovante
- Status inicial de revisão: `PENDING`

### Teste rápido

1. Como jogador, acesse `/player/results/new`.
2. Envie resultado para um torneio da sua oferta.
3. Verifique no banco que `match_results.review_status = 'PENDING'`.

## Fase 3 - Revisão admin + distribuição

### O que foi implementado

- Tela admin de revisão: `/admin/results`
- Aprovar/rejeitar resultado
- Distribuição automática na aprovação:
  - Apoiador proporcional ao percentual comprado
  - Taxa plataforma = 8% sobre ganho do apoiador
  - Restante para jogador
- Auditoria em `prize_distributions`

### Teste rápido

1. Como admin, acesse `/admin/results`.
2. Abra um resultado pendente.
3. Clique em `Aprovar e distribuir`.
4. Confirme no banco:
   - `match_results.review_status = 'APPROVED'`
   - linhas em `prize_distributions`
   - carteiras atualizadas em `wallets`

## Fase 4 - Escrow e estorno

### O que foi implementado

- Estado de escrow por oferta:
  - `stake_offers.escrow_status` (`COLLECTING`, `COMPLETE`, `REFUNDED`)
- Tabela de escrow por torneio/oferta: `tournament_escrows`
- Liberação ao jogador quando atinge integral:
  - ao completar `total_required`, escrow vira `COMPLETE`
- Estorno:
  - automático para escrows em `COLLECTING` expirados (ao consultar status)
  - manual por endpoint (admin ou dono da oferta)
- Rotas:
  - `GET /api/escrow/{offer_id}/status`
  - `POST /api/escrow/{offer_id}/refund`

### Teste rápido

1. Crie oferta com data/hora definida.
2. Faça investimentos até completar o valor requerido.
3. Consulte `/api/escrow/{offer_id}/status`:
   - deve aparecer `COMPLETE`.
4. Para estorno manual:
   - chame `POST /api/escrow/{offer_id}/refund`.
5. Para estorno automático de não-completo:
   - deixe prazo expirar e consulte o status.

## Fase 5 - Timer e notificações

### O que foi implementado

- Modelo `notifications`
- Job de prazo de resultado (`services/jobs.py` + `services/notification_jobs.py`)
- Lembrete para envio de resultado antes de 12h
- Bloqueio automático por não envio em 12h
- Caixa de notificações no front (header)
- Endpoints:
  - `GET /api/notifications`
  - `GET /api/notifications/unread-count`
  - `POST /api/notifications/{id}/read`
  - `POST /api/jobs/run-deadlines` (admin, execução manual)

### Teste rápido

1. Deixe um torneio vencer sem `match_result`.
2. Acesse dashboard/home para disparar processamento de jobs.
3. Abra o sino de notificações:
   - deve existir lembrete de prazo.
4. Após o marco de 12h:
   - usuário deve ficar bloqueado (`users.is_blocked = true`).
5. Envie resultado em `/player/results/new`:
   - bloqueio por esse motivo deve ser removido.

## Fase 6 - Agendamento de call (simples)

### O que foi implementado

- Modelo `call_schedules` com status:
  - `PENDING`
  - `CONFIRMED`
  - `CANCELLED`
- Tela do usuário:
  - `GET /player/calls`
  - `POST /player/calls`
- Tela/admin:
  - `GET /admin/calls`
  - `POST /admin/calls/{schedule_id}/status`

### Teste rápido

1. Como usuário, abrir `/player/calls`.
2. Solicitar uma call para uma data futura.
3. Como admin, abrir `/admin/calls`.
4. Alterar status para `CONFIRMED` e opcionalmente preencher `call_link`.
5. Voltar ao usuário e validar status atualizado.

## 5) Ações obrigatórias suas

1. Instalar dependências no seu ambiente (principalmente `sqlalchemy` e `python-multipart`).
2. Atualizar schema do banco (reset via `seed.py` ou migração manual).
3. Garantir conta admin (`tipo='admin'`) para KYC e revisão.
4. Garantir escrita em `static/uploads/`.

Sem esses 4 passos, as fases podem compilar mas não funcionar completamente em runtime.

## 6) Endpoints novos consolidados

- Admin/KYC:
  - `GET /admin/dashboard`
  - `POST /admin/kyc/{document_id}/approve`
  - `POST /admin/kyc/{document_id}/reject`
- Resultados:
  - `GET /player/results/new`
  - `POST /player/results`
  - `GET /admin/results`
  - `POST /admin/results/{result_id}/approve`
  - `POST /admin/results/{result_id}/reject`
- Escrow:
  - `GET /api/escrow/{offer_id}/status`
  - `POST /api/escrow/{offer_id}/refund`

## 7) Próximo passo recomendado

Após estabilizar este guia em ambiente local, seguir para:

- ajustes finais de UX e testes integrados.

