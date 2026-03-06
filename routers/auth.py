import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Security
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import APIKeyCookie
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from db import get_db
from models import EmailVerificationCode, User, UserDocument, Wallet
from services.email_sender import send_verification_code_email

templates = Jinja2Templates(directory="templates")
router = APIRouter()

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
session_cookie = APIKeyCookie(name="safe_stake_session", auto_error=False)
REGISTER_PURPOSE = "register"
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def get_env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def get_env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


REGISTER_CODE_TTL_MINUTES = get_env_int("REGISTER_CODE_TTL_MINUTES", 10)
REGISTER_MAX_ATTEMPTS = get_env_int("REGISTER_MAX_ATTEMPTS", 5)
REGISTER_REQUIRE_EMAIL_VERIFICATION = get_env_bool("REGISTER_REQUIRE_EMAIL_VERIFICATION", True)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def normalize_document(value: str) -> str:
    return "".join(char for char in value if char.isdigit())


def normalize_phone(value: str) -> str:
    return "".join(char for char in value if char.isdigit())


def format_phone(value: str) -> str:
    digits = normalize_phone(value)[:11]
    if len(digits) >= 11:
        return f"({digits[:2]}) {digits[2:7]}-{digits[7:11]}"
    if len(digits) >= 10:
        return f"({digits[:2]}) {digits[2:6]}-{digits[6:10]}"
    return digits


def mask_email(value: str) -> str:
    if "@" not in value:
        return value
    local, domain = value.split("@", maxsplit=1)
    if len(local) <= 2:
        masked_local = local[0] + "*" if local else "*"
    else:
        masked_local = local[:2] + "*" * (len(local) - 2)
    return f"{masked_local}@{domain}"


def is_valid_cpf(cpf: str) -> bool:
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    total = sum(int(cpf[i]) * (10 - i) for i in range(9))
    first_digit = ((total * 10) % 11) % 10
    if first_digit != int(cpf[9]):
        return False
    total = sum(int(cpf[i]) * (11 - i) for i in range(10))
    second_digit = ((total * 10) % 11) % 10
    return second_digit == int(cpf[10])


def is_valid_cnpj(cnpj: str) -> bool:
    if len(cnpj) != 14 or cnpj == cnpj[0] * 14:
        return False
    w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    w2 = [6] + w1
    total = sum(int(cnpj[i]) * w1[i] for i in range(12))
    d1 = 11 - (total % 11)
    d1 = 0 if d1 >= 10 else d1
    total = sum(int(cnpj[i]) * w2[i] for i in range(13))
    d2 = 11 - (total % 11)
    d2 = 0 if d2 >= 10 else d2
    return d1 == int(cnpj[12]) and d2 == int(cnpj[13])


def is_valid_cpf_cnpj(document: str) -> bool:
    if len(document) == 11:
        return is_valid_cpf(document)
    if len(document) == 14:
        return is_valid_cnpj(document)
    return False


def is_strong_password(password: str) -> bool:
    if len(password) < 8:
        return False
    has_letter = any(char.isalpha() for char in password)
    has_digit = any(char.isdigit() for char in password)
    return has_letter and has_digit


def get_verification_secret() -> str:
    return (
        os.getenv("VERIFICATION_CODE_SECRET")
        or os.getenv("SESSION_SECRET")
        or os.getenv("SECRET_KEY")
        or "safe-stake-dev-secret"
    )


def hash_verification_code(email: str, purpose: str, code: str) -> str:
    payload = f"{get_verification_secret()}:{email.lower()}:{purpose}:{code}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_pending_verification_from_session(request: Request, db: Session) -> EmailVerificationCode | None:
    verification_id = request.session.get("pending_verification_id")
    if not verification_id:
        return None
    stmt = select(EmailVerificationCode).where(
        EmailVerificationCode.id == verification_id,
        EmailVerificationCode.purpose == REGISTER_PURPOSE,
        EmailVerificationCode.consumed_at.is_(None),
    )
    verification = db.execute(stmt).scalars().first()
    if not verification:
        request.session.pop("pending_verification_id", None)
        return None
    now = datetime.now(timezone.utc)
    if verification.expires_at < now:
        verification.consumed_at = now
        db.commit()
        request.session.pop("pending_verification_id", None)
        return None
    return verification


def render_register(
    request: Request,
    error: str | None = None,
    form_data: dict | None = None,
):
    return templates.TemplateResponse(
        "register.html",
        {
            "request": request,
            "user": None,
            "wallet": None,
            "error": error,
            "form_data": form_data or {"tipo": "apoiador"},
        },
        status_code=400 if error else 200,
    )


def render_register_verify(
    request: Request,
    verification: EmailVerificationCode,
    error: str | None = None,
    info: str | None = None,
):
    return templates.TemplateResponse(
        "register_verify.html",
        {
            "request": request,
            "user": None,
            "wallet": None,
            "error": error,
            "info": info,
            "email_masked": mask_email(verification.email),
        },
        status_code=400 if error else 200,
    )


def fetch_current_user(
    request: Request,
    db: Session,
    _session_cookie: str | None = Security(session_cookie),
) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    stmt = select(User).where(User.id == user_id).options(joinedload(User.wallet))
    user = db.execute(stmt).scalars().first()
    if not user:
        # Evita sessão inválida "presa" em user inexistente.
        request.session.pop("user_id", None)
    return user


def get_wallet_summary(user: User | None) -> dict | None:
    if not user or not user.wallet:
        return None
    return {
        "saldo_disponivel": user.wallet.saldo_disponivel,
        "saldo_em_jogo": user.wallet.saldo_em_jogo,
    }


def get_latest_user_document(user_id: int, db: Session) -> UserDocument | None:
    stmt = (
        select(UserDocument)
        .where(UserDocument.user_id == user_id)
        .order_by(UserDocument.created_at.desc(), UserDocument.id.desc())
    )
    return db.execute(stmt).scalars().first()


def is_user_kyc_approved(user: User | None, db: Session) -> bool:
    if not user:
        return False
    return True


def ensure_user_kyc_approved(user: User | None, db: Session) -> None:
    if is_user_kyc_approved(user, db):
        return
    raise HTTPException(status_code=403, detail="Seu KYC ainda não foi aprovado pelo admin.")


def ensure_admin_user(user: User | None) -> None:
    if not user or user.tipo != "admin":
        raise HTTPException(status_code=403, detail="Acesso restrito ao administrador.")


def ensure_user_not_blocked(user: User | None) -> None:
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para continuar.")
    if user.is_blocked:
        detail = user.blocked_reason or "Conta bloqueada. Contate o suporte/admin."
        raise HTTPException(status_code=403, detail=detail)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=303)
    info = None
    if request.query_params.get("registered") == "1":
        info = "Cadastro confirmado com sucesso. Entre com seu email e senha."
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "user": user, "wallet": get_wallet_summary(user), "info": info},
    )


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_form(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "user": None,
            "wallet": None,
            "show_forgot_password": True,
        },
    )


@router.post("/forgot-password", response_class=HTMLResponse)
def forgot_password_submit(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=303)

    normalized_email = email.strip().lower()
    _ = db.execute(select(User.id).where(User.email == normalized_email)).scalars().first()
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "user": None,
            "wallet": None,
            "info": "Se o email existir, enviaremos instruções de recuperação.",
            "show_forgot_password": True,
            "email": normalized_email,
        },
    )


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    senha: str = Form(...),
    db: Session = Depends(get_db),
):
    normalized_email = email.strip().lower()
    stmt = select(User).where(User.email == normalized_email)
    user = db.execute(stmt).scalars().first()
    if not user or not verify_password(senha, user.password_hash):
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Credenciais inválidas.",
                "email": normalized_email,
                "user": None,
                "wallet": None,
            },
            status_code=401,
        )

    request.session["user_id"] = user.id
    return RedirectResponse(url="/dashboard", status_code=303)


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=303)
    return render_register(request)


@router.post("/register", response_class=HTMLResponse)
async def register(
    request: Request,
    nome: str = Form(...),
    email: str = Form(...),
    pix_key: str = Form(...),
    senha: str = Form(...),
    confirmar_senha: str = Form(...),
    db: Session = Depends(get_db),
):
    form_data = {
        "nome": nome,
        "email": email,
        "pix_key": pix_key,
    }

    normalized_email = email.strip().lower()
    normalized_pix_key = pix_key.strip()
    form_data["email"] = normalized_email
    form_data["pix_key"] = normalized_pix_key

    if not EMAIL_PATTERN.match(normalized_email):
        return render_register(request, "Informe um email válido.", form_data)
    if not normalized_pix_key:
        return render_register(request, "A Chave Pix é obrigatória para cadastro.", form_data)
    if not is_strong_password(senha):
        return render_register(request, "A senha deve ter 8+ caracteres, letras e números.", form_data)
    if senha != confirmar_senha:
        return render_register(request, "A confirmação da senha não confere.", form_data)

    exists_stmt = select(User).where(User.email == normalized_email)
    existing_user = db.execute(exists_stmt).scalars().first()
    if existing_user:
        return render_register(request, "Este email já está cadastrado.", form_data)

    if not REGISTER_REQUIRE_EMAIL_VERIFICATION:
        new_user = User(
            nome=nome.strip(),
            email=normalized_email,
            password_hash=get_password_hash(senha),
            tipo="apoiador",
            pix_key=normalized_pix_key,
            sharkscope_link=None,
            endereco_completo=None,
            data_nascimento=None,
            bio=None,
            is_verified=True,
        )
        db.add(new_user)
        db.flush()
        db.add(
            Wallet(
                user_id=new_user.id,
                saldo_disponivel=Decimal("0"),
                saldo_bloqueado=Decimal("0"),
                saldo_em_jogo=Decimal("0"),
            )
        )
        db.commit()
        return RedirectResponse(url="/login?registered=1", status_code=303)

    now = datetime.now(timezone.utc)
    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = hash_verification_code(normalized_email, REGISTER_PURPOSE, code)
    registration_payload = json.dumps(
        {
            "nome": nome.strip(),
            "email": normalized_email,
            "password_hash": get_password_hash(senha),
            "tipo": "apoiador",
            "pix_key": normalized_pix_key,
            "sharkscope_link": None,
            "endereco_completo": None,
            "data_nascimento": None,
            "bio": None,
        },
        ensure_ascii=True,
    )

    active_codes_stmt = select(EmailVerificationCode).where(
        EmailVerificationCode.email == normalized_email,
        EmailVerificationCode.purpose == REGISTER_PURPOSE,
        EmailVerificationCode.consumed_at.is_(None),
    )
    for item in db.execute(active_codes_stmt).scalars().all():
        item.consumed_at = now

    verification = EmailVerificationCode(
        email=normalized_email,
        purpose=REGISTER_PURPOSE,
        code_hash=code_hash,
        registration_payload=registration_payload,
        attempts=0,
        max_attempts=REGISTER_MAX_ATTEMPTS,
        expires_at=now + timedelta(minutes=REGISTER_CODE_TTL_MINUTES),
        consumed_at=None,
        created_at=now,
    )
    db.add(verification)
    db.commit()
    db.refresh(verification)
    request.session["pending_verification_id"] = verification.id

    email_sent = send_verification_code_email(normalized_email, code)
    if email_sent:
        return RedirectResponse(url="/register/verify", status_code=303)

    allow_fallback = os.getenv("ALLOW_LOCAL_EMAIL_FALLBACK", "true").strip().lower() in {"1", "true", "yes", "on"}
    if allow_fallback:
        return render_register_verify(
            request,
            verification,
            info=f"Modo teste: use o código {code} para validar seu cadastro.",
        )
    return render_register_verify(
        request,
        verification,
        info=(
            "Cadastro iniciado, mas não conseguimos enviar o email de verificação agora. "
            "Tente reenviar o código em instantes."
        ),
    )


@router.get("/register/verify", response_class=HTMLResponse)
def register_verify_form(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=303)
    verification = get_pending_verification_from_session(request, db)
    if not verification:
        return RedirectResponse(url="/register", status_code=303)
    return render_register_verify(request, verification)


@router.post("/register/verify", response_class=HTMLResponse)
def register_verify_submit(
    request: Request,
    code: str = Form(...),
    db: Session = Depends(get_db),
):
    verification = get_pending_verification_from_session(request, db)
    if not verification:
        return RedirectResponse(url="/register", status_code=303)

    code_digits = "".join(char for char in code if char.isdigit())
    if len(code_digits) != 6:
        return render_register_verify(request, verification, error="Informe o código de 6 dígitos.")

    now = datetime.now(timezone.utc)
    if verification.attempts >= verification.max_attempts:
        verification.consumed_at = now
        db.commit()
        request.session.pop("pending_verification_id", None)
        return render_register(request, "Muitas tentativas. Refaça seu cadastro.")

    submitted_hash = hash_verification_code(verification.email, verification.purpose, code_digits)
    if submitted_hash != verification.code_hash:
        verification.attempts += 1
        attempts_left = verification.max_attempts - verification.attempts
        if attempts_left <= 0:
            verification.consumed_at = now
            db.commit()
            request.session.pop("pending_verification_id", None)
            return render_register(request, "Código inválido. Refaça seu cadastro.")
        db.commit()
        return render_register_verify(
            request,
            verification,
            error=f"Código inválido. Restam {attempts_left} tentativa(s).",
        )

    try:
        payload = json.loads(verification.registration_payload or "{}")
    except json.JSONDecodeError:
        verification.consumed_at = now
        db.commit()
        request.session.pop("pending_verification_id", None)
        return render_register(request, "Cadastro pendente inválido. Refaça seu cadastro.")

    email = str(payload.get("email", "")).strip().lower()
    exists_stmt = select(User).where(User.email == email)
    existing_user = db.execute(exists_stmt).scalars().first()
    if existing_user:
        verification.consumed_at = now
        db.commit()
        request.session.pop("pending_verification_id", None)
        return render_register(request, "Email já foi cadastrado.")

    new_user = User(
        nome=str(payload.get("nome", "")).strip(),
        email=email,
        password_hash=str(payload.get("password_hash", "")),
        tipo="apoiador",
        pix_key=str(payload.get("pix_key", "")).strip() or None,
        sharkscope_link=None,
        endereco_completo=payload.get("endereco_completo"),
        data_nascimento=payload.get("data_nascimento"),
        bio=payload.get("bio"),
        is_verified=True,
    )
    db.add(new_user)
    db.flush()
    db.add(
        Wallet(
            user_id=new_user.id,
            saldo_disponivel=Decimal("0"),
            saldo_bloqueado=Decimal("0"),
            saldo_em_jogo=Decimal("0"),
        )
    )
    verification.consumed_at = now
    db.commit()

    request.session.pop("pending_verification_id", None)
    return RedirectResponse(url="/login?registered=1", status_code=303)


@router.post("/register/resend-code", response_class=HTMLResponse)
def register_resend_code(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=303)
    verification = get_pending_verification_from_session(request, db)
    if not verification:
        return RedirectResponse(url="/register", status_code=303)

    code = f"{secrets.randbelow(1_000_000):06d}"
    verification.code_hash = hash_verification_code(verification.email, verification.purpose, code)
    verification.attempts = 0
    verification.expires_at = datetime.now(timezone.utc) + timedelta(minutes=REGISTER_CODE_TTL_MINUTES)
    db.commit()

    sent = send_verification_code_email(verification.email, code)
    if sent:
        return render_register_verify(request, verification, info="Enviamos um novo código para seu email.")

    allow_fallback = os.getenv("ALLOW_LOCAL_EMAIL_FALLBACK", "true").strip().lower() in {"1", "true", "yes", "on"}
    if allow_fallback:
        return render_register_verify(
            request,
            verification,
            info=f"Modo teste: novo código {code}.",
        )
    return render_register_verify(request, verification, error="Não foi possível reenviar o código.")


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)
