from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_engine, get_sessionmaker
from models import Base, Investment, StakeOffer, Tournament, User, Wallet
from routers.auth import get_password_hash


def seed_users(db: Session) -> tuple[User, User]:
    admin = User(
        nome="Rodrigo Alves",
        email="rodrigo@safestake.com",
        password_hash=get_password_hash("123"),
        tipo="admin",
    )
    admin.wallet = Wallet(saldo_disponivel=Decimal("5000.00"), saldo_em_jogo=Decimal("0"))

    player = User(
        nome="Murilo Almeida",
        email="murilo@safestake.com",
        password_hash=get_password_hash("123"),
        tipo="jogador",
    )
    player.wallet = Wallet(saldo_disponivel=Decimal("0.00"), saldo_em_jogo=Decimal("0"))

    db.add_all([admin, player])
    db.flush()
    return admin, player


def seed_tournaments(db: Session, player: User) -> None:
    now = datetime.utcnow()
    tournaments = [
        Tournament(
            nome="Sunday Million",
            buyin=Decimal("215.00"),
            data_hora=now + timedelta(days=1, hours=3),
            status="Aberto",
            sharkscope_id="PokerStars",
            plataforma="PokerStars",
        ),
        Tournament(
            nome="Bounty Builder",
            buyin=Decimal("55.00"),
            data_hora=now + timedelta(days=2, hours=6),
            status="Aberto",
            sharkscope_id="GGPoker",
            plataforma="GGPoker",
        ),
        Tournament(
            nome="High Roller Turbo",
            buyin=Decimal("109.00"),
            data_hora=now + timedelta(days=3, hours=4),
            status="Aberto",
            sharkscope_id="888poker",
            plataforma="888poker",
        ),
    ]
    db.add_all(tournaments)
    db.flush()

    offers = [
        StakeOffer(
            tournament_id=tournaments[0].id,
            player_id=player.id,
            markup=Decimal("1.10"),
            total_disponivel_pct=Decimal("70.00"),
            vendido_pct=Decimal("20.00"),
        ),
        StakeOffer(
            tournament_id=tournaments[1].id,
            player_id=player.id,
            markup=Decimal("1.05"),
            total_disponivel_pct=Decimal("60.00"),
            vendido_pct=Decimal("10.00"),
        ),
        StakeOffer(
            tournament_id=tournaments[2].id,
            player_id=player.id,
            markup=Decimal("1.08"),
            total_disponivel_pct=Decimal("80.00"),
            vendido_pct=Decimal("35.00"),
        ),
    ]
    db.add_all(offers)


def seed_investment(db: Session, backer: User, offer: StakeOffer) -> None:
    investment = Investment(
        offer_id=offer.id,
        backer_id=backer.id,
        valor_investido=Decimal("200.00"),
        pct_comprada=Decimal("2.00"),
        lucro_recebido=Decimal("0.00"),
    )
    db.add(investment)


def seed():
    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        admin, player = seed_users(db)
        seed_tournaments(db, player)
        first_offer = db.execute(select(StakeOffer)).scalars().first()
        if first_offer:
            seed_investment(db, admin, first_offer)
        db.commit()


if __name__ == "__main__":
    seed()
    print("Seed concluído com sucesso.")
