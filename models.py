from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nome: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(180), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    tipo: Mapped[str] = mapped_column(
        Enum("jogador", "apoiador", "admin", name="user_tipo"),
        nullable=False,
    )
    sharkscope_link: Mapped[str | None] = mapped_column(String(255))
    pix_key: Mapped[str | None] = mapped_column(String(120))

    wallet: Mapped["Wallet"] = relationship(back_populates="user", uselist=False)


class Wallet(Base):
    __tablename__ = "wallets"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    saldo_disponivel: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    saldo_em_jogo: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)

    user: Mapped[User] = relationship(back_populates="wallet")


class Tournament(Base):
    __tablename__ = "tournaments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sharkscope_id: Mapped[str | None] = mapped_column(String(80))
    nome: Mapped[str] = mapped_column(String(160), nullable=False)
    buyin: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    data_hora: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        Enum("Aberto", "Jogando", "Finalizado", name="tournament_status"),
        default="Aberto",
    )

    offers: Mapped[list["StakeOffer"]] = relationship(back_populates="tournament")


class StakeOffer(Base):
    __tablename__ = "stake_offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(ForeignKey("tournaments.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    markup: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=1.0)
    total_disponivel_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=100)
    vendido_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0)

    tournament: Mapped[Tournament] = relationship(back_populates="offers")
    player: Mapped[User] = relationship()
    investments: Mapped[list["Investment"]] = relationship(back_populates="offer")


class Investment(Base):
    __tablename__ = "investments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("stake_offers.id"))
    backer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    valor_investido: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    pct_comprada: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    lucro_recebido: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)

    offer: Mapped[StakeOffer] = relationship(back_populates="investments")
    backer: Mapped[User] = relationship()
