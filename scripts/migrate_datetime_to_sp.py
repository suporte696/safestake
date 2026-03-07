"""
Migra tournaments.data_hora e tournament_escrows.deadline_at para
TIMESTAMP WITHOUT TIME ZONE, com valores em horário de São Paulo.

Assim, o que o usuário escolhe (ex.: 19h) é salvo e exibido como 19h,
sem conversão no front.

Rodar: python scripts/migrate_datetime_to_sp.py
Requer: DATABASE_URL no ambiente ou .env
"""
import os
import sqlalchemy
from sqlalchemy import text


def run():
    database_url = os.getenv("DATABASE_URL") or "postgresql://user:pass@localhost/safestake"
    engine = sqlalchemy.create_engine(database_url)
    with engine.connect() as conn:
        conn.execute(text("SET timezone = 'America/Sao_Paulo'"))
        conn.execute(
            text("""
                ALTER TABLE tournaments
                ALTER COLUMN data_hora TYPE TIMESTAMP WITHOUT TIME ZONE
                USING data_hora AT TIME ZONE 'UTC' AT TIME ZONE 'America/Sao_Paulo'
            """)
        )
        conn.execute(
            text("""
                ALTER TABLE tournament_escrows
                ALTER COLUMN deadline_at TYPE TIMESTAMP WITHOUT TIME ZONE
                USING deadline_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Sao_Paulo'
            """)
        )
        conn.commit()
    print("Migração concluída: data_hora e deadline_at agora são horário de São Paulo (naive).")


if __name__ == "__main__":
    run()
