# Migrações de banco de dados

Este arquivo registra **alterações de schema** (tabelas, colunas, enums) necessárias para as funcionalidades do checklist. Use-o para rodar migrações **localmente** ou **no servidor** quando implementar os itens indicados.

---

## Alterações já feitas (até 06/03/2026)

**Nenhuma.** (Migração de horário para SP — ver abaixo — deve ser rodada manualmente quando for aplicar a convenção “sempre SP no banco”.)

---

## Migrações futuras (rodar quando implementar o item)

Só execute o bloco correspondente quando for implementar a funcionalidade descrita. As tabelas e enums já existem; abaixo estão apenas **adições** (ALTER TABLE / ALTER TYPE).

---

### [ ] Horário sempre em São Paulo (data_hora / deadline_at)

**Quando:** Ao adotar a convenção de que **toda hora de torneio/escrow** é armazenada em **horário de São Paulo** no banco (naive), para que o front não precise converter: usuário escolhe 19h → salva 19h → exibe 19h.

**O que muda:** As colunas `tournaments.data_hora` e `tournament_escrows.deadline_at` passam de `TIMESTAMP WITH TIME ZONE` para `TIMESTAMP WITHOUT TIME ZONE`. O valor armazenado passa a ser sempre “horário local de São Paulo” (ex.: 19:00 = 19h em SP).

**Importante:** Antes do `ALTER`, defina a timezone da sessão como `America/Sao_Paulo`, para que os valores já existentes (em UTC) sejam convertidos para o horário de SP ao mudar o tipo.

```sql
-- PostgreSQL: rodar em uma única sessão
SET timezone = 'America/Sao_Paulo';

ALTER TABLE tournaments
  ALTER COLUMN data_hora TYPE TIMESTAMP WITHOUT TIME ZONE
  USING data_hora AT TIME ZONE 'UTC' AT TIME ZONE 'America/Sao_Paulo';

ALTER TABLE tournament_escrows
  ALTER COLUMN deadline_at TYPE TIMESTAMP WITHOUT TIME ZONE
  USING deadline_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Sao_Paulo';
```

**Nota:** Se os dados atuais já estiverem armazenados como “horário de São Paulo” em coluna WITH TIME ZONE, use apenas:

```sql
SET timezone = 'America/Sao_Paulo';
ALTER TABLE tournaments ALTER COLUMN data_hora TYPE TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE tournament_escrows ALTER COLUMN deadline_at TYPE TIMESTAMP WITHOUT TIME ZONE;
```

**Modelo (`models.py`):** `Tournament.data_hora` e `TournamentEscrow.deadline_at` já estão como `DateTime(timezone=False)`.

---

### [ ] Item 24 — Saldo Pendente (opcional)

**Quando:** Ao implementar “Saldo Pendente” no painel do apoiador (além de recebido, investido e carteira), **se** decidir armazenar o valor na tabela `wallets` em vez de calcular na hora.

**Se for só cálculo em tempo real** (soma de `lucro_recebido` dos investments com `payout_status = 'PENDING'`), **não é necessário** rodar migração.

**Se for coluna em `wallets`:**

```sql
-- PostgreSQL
ALTER TABLE wallets
ADD COLUMN IF NOT EXISTS saldo_pendente NUMERIC(12, 2) NOT NULL DEFAULT 0;

COMMENT ON COLUMN wallets.saldo_pendente IS 'Valor a receber (lucro já distribuído, aguardando marcar como pago).';
```

**Atualizar modelo em `models.py`:**

```python
# Em Wallet, adicionar:
saldo_pendente: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
```

**Script Python (opcional)** para rodar no ambiente local ou no servidor:

```python
# scripts/migrate_saldo_pendente.py
import os
import sqlalchemy
from sqlalchemy import text

def run():
    database_url = os.getenv("DATABASE_URL") or "postgresql://user:pass@localhost/safestake"
    engine = sqlalchemy.create_engine(database_url)
    with engine.connect() as conn:
        conn.execute(text("""
            ALTER TABLE wallets
            ADD COLUMN IF NOT EXISTS saldo_pendente NUMERIC(12, 2) NOT NULL DEFAULT 0
        """))
        conn.commit()
    print("Coluna saldo_pendente aplicada (ou já existia).")

if __name__ == "__main__":
    run()
```

---

### [x] Item 31 — Status "Em revisão" para informes de ganho (obrigatório neste deploy)

**Quando:** Neste deploy, o cliente pediu separar semanticamente `PENDING` e `UNDER_REVIEW`. Então a migração do enum é **obrigatória**.

**Rodar no PostgreSQL:**

```sql
-- PostgreSQL (enum já existe: match_result_review_status)
ALTER TYPE match_result_review_status ADD VALUE IF NOT EXISTS 'UNDER_REVIEW';

UPDATE match_results
SET review_status = 'UNDER_REVIEW'
WHERE review_status = 'PENDING';
```

**Atenção:** Em PostgreSQL, `ADD VALUE` não pode ser revertido facilmente.

**Script Python (recomendado):**

```bash
python scripts/migrate_match_result_under_review.py
```

---

### Resumo

| Item | Obrigatório? | O que rodar |
|------|--------------|-------------|
| 24 (Saldo Pendente) | Só se usar coluna em `wallets` | `ALTER TABLE wallets ADD COLUMN saldo_pendente ...` ou script Python acima |
| 31 (Em revisão) | Sim (neste deploy) | `ALTER TYPE ... ADD VALUE 'UNDER_REVIEW'` + `UPDATE match_results ...` (ou `python scripts/migrate_match_result_under_review.py`) |

---

## Como rodar

- **Local / servidor com acesso ao banco:** use `psql` ou outro cliente com o SQL acima.
- **Script Python:** crie `scripts/migrate_saldo_pendente.py` com o conteúdo da seção do item 24, ajuste `DATABASE_URL` (ou `.env`) e execute:  
  `python scripts/migrate_saldo_pendente.py`

Sempre faça backup do banco antes de rodar migrações em produção.
