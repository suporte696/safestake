#!/usr/bin/env python3
"""
Migração: adiciona status UNDER_REVIEW no enum match_result_review_status
e converte registros existentes de PENDING para UNDER_REVIEW.

Uso:
  export DATABASE_URL="postgresql://user:pass@host/dbname"
  python scripts/migrate_match_result_under_review.py
"""
import os
import sys


def run() -> None:
    try:
        from sqlalchemy import create_engine, text
    except ImportError:
        print("Instale: pip install sqlalchemy")
        sys.exit(1)

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("Defina DATABASE_URL (ou carregue o .env do projeto).")
        sys.exit(1)

    engine = create_engine(database_url)
    with engine.connect() as conn:
        conn.execute(
            text(
                """
                ALTER TYPE match_result_review_status
                ADD VALUE IF NOT EXISTS 'UNDER_REVIEW'
                """
            )
        )
        conn.execute(
            text(
                """
                UPDATE match_results
                SET review_status = 'UNDER_REVIEW'
                WHERE review_status = 'PENDING'
                """
            )
        )
        conn.commit()
    print("OK: enum atualizado e registros PENDING migrados para UNDER_REVIEW.")


if __name__ == "__main__":
    run()
