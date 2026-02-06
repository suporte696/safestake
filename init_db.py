from db import get_engine
from models import Base


def init_db() -> None:
    engine = get_engine()
    Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    init_db()
    print("Tabelas criadas com sucesso.")
