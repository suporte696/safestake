#!/usr/bin/env python3
"""
Migração: adiciona coluna saldo_pendente na tabela wallets (item 24 do checklist).

Rodar apenas quando for implementar "Saldo Pendente" no painel do apoiador
e decidir armazenar o valor na tabela. Pode ser rodado localmente ou no servidor.

Uso:
  export DATABASE_URL="postgresql://user:pass@host/dbname"   # ou use .env
  python scripts/migrate_saldo_pendente.py
"""
import os
import sys

# Permite importar do projeto (raiz no path)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def run():
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
    sql = """
        ALTER TABLE wallets
        ADD COLUMN IF NOT EXISTS saldo_pendente NUMERIC(12, 2) NOT NULL DEFAULT 0
    """
    with engine.connect() as conn:
        conn.execute(text(sql))
        conn.commit()
    print("OK: Coluna wallets.saldo_pendente aplicada (ou já existia).")


if __name__ == "__main__":
    run()
