from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, validates

from constants import SUPPORTED_ROOMS


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
    cpf_cnpj: Mapped[str | None] = mapped_column(String(14), unique=True)
    telefone: Mapped[str | None] = mapped_column(String(20))
    endereco_completo: Mapped[str | None] = mapped_column(String(255))
    data_nascimento: Mapped[date | None] = mapped_column(Date)
    bio: Mapped[str | None] = mapped_column(String(255))
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    wallet: Mapped["Wallet"] = relationship(back_populates="user", uselist=False)
    crypto_transactions: Mapped[list["CryptoTransaction"]] = relationship(back_populates="user")


class EmailVerificationCode(Base):
    __tablename__ = "email_verification_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    registration_payload: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Wallet(Base):
    __tablename__ = "wallets"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    saldo_disponivel: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    saldo_bloqueado: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)  # propostas pendentes (bids)
    saldo_em_jogo: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)

    user: Mapped[User] = relationship(back_populates="wallet")


class Tournament(Base):
    __tablename__ = "tournaments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sharkscope_id: Mapped[str | None] = mapped_column(String(80))
    nome: Mapped[str] = mapped_column(String(160), nullable=False)
    plataforma: Mapped[str] = mapped_column(String(80), nullable=False)
    sharkscope_link: Mapped[str | None] = mapped_column(String(255))
    buyin: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    data_hora: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        Enum("Aberto", "Jogando", "Finalizado", name="tournament_status"),
        default="Aberto",
    )

    offers: Mapped[list["StakeOffer"]] = relationship(back_populates="tournament")
    results: Mapped[list["MatchResult"]] = relationship(back_populates="tournament")

    @validates("plataforma")
    def validate_plataforma(self, _key: str, value: str) -> str:
        if value not in SUPPORTED_ROOMS:
            raise ValueError("Plataforma não suportada pelo SharkScope.")
        return value


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
    bids: Mapped[list["StakeBid"]] = relationship(back_populates="offer")


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


class StakeBid(Base):
    __tablename__ = "stake_bids"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("stake_offers.id"), nullable=False)
    backer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    proposed_markup: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum("PENDING", "ACCEPTED", "REJECTED", "CANCELLED", name="stake_bid_status"),
        nullable=False,
        default="PENDING",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    offer: Mapped[StakeOffer] = relationship(back_populates="bids")
    backer: Mapped[User] = relationship()


class CryptoTransaction(Base):
    __tablename__ = "crypto_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    coingate_order_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    amount_fiat: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)  # BRL
    amount_crypto: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    currency_crypto: Mapped[str | None] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(
        Enum("PAID", "PENDING", "EXPIRED", name="crypto_tx_status"),
        nullable=False,
        default="PENDING",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    user: Mapped[User] = relationship(back_populates="crypto_transactions")


class MatchResult(Base):
    __tablename__ = "match_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(ForeignKey("tournaments.id"), nullable=False)
    player_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    posicao_final: Mapped[int] = mapped_column(Integer, nullable=False)
    valor_premio: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    print_url: Mapped[str | None] = mapped_column(String(255))
    admin_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    tournament: Mapped[Tournament] = relationship(back_populates="results")
    player: Mapped[User] = relationship()
