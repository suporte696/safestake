import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, Request, Security
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import APIKeyCookie
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from db import get_db
from models import User, Wallet
from services.email_sender import send_verification_code_email

templates = Jinja2Templates(directory="templates")
router = APIRouter()

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
session_cookie = APIKeyCookie(name="safe_stake_session", auto_error=False)
PENDING_REGISTRATIONS: dict[str, dict] = {}
REGISTER_CODE_TTL_MINUTES = 10
REGISTER_MAX_ATTEMPTS = 5
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


def cleanup_pending_registrations() -> None:
    now = datetime.now(timezone.utc)
    expired_tokens = [
        token
        for token, payload in PENDING_REGISTRATIONS.items()
        if payload["expires_at"] < now
    ]
    for token in expired_tokens:
        PENDING_REGISTRATIONS.pop(token, None)


def get_pending_registration_from_session(request: Request) -> dict | None:
    cleanup_pending_registrations()
    token = request.session.get("pending_register_token")
    if not token:
        return None
    payload = PENDING_REGISTRATIONS.get(token)
    if not payload:
        request.session.pop("pending_register_token", None)
        return None
    if payload["expires_at"] < datetime.now(timezone.utc):
        PENDING_REGISTRATIONS.pop(token, None)
        request.session.pop("pending_register_token", None)
        return None
    return payload


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
            "form_data": form_data or {"tipo": "jogador"},
        },
        status_code=400 if error else 200,
    )


def render_register_verify(
    request: Request,
    pending: dict,
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
            "email_masked": mask_email(pending["email"]),
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
    return db.execute(stmt).scalars().first()


def get_wallet_summary(user: User | None) -> dict | None:
    if not user or not user.wallet:
        return None
    return {
        "saldo_disponivel": user.wallet.saldo_disponivel,
        "saldo_em_jogo": user.wallet.saldo_em_jogo,
    }


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
def register(
    request: Request,
    nome: str = Form(...),
    email: str = Form(...),
    cpf_cnpj: str = Form(...),
    telefone: str = Form(...),
    senha: str = Form(...),
    confirmar_senha: str = Form(...),
    tipo: str = Form(...),
    sharkscope_link: str = Form(""),
    db: Session = Depends(get_db),
):
    form_data = {
        "nome": nome,
        "email": email,
        "cpf_cnpj": cpf_cnpj,
        "telefone": telefone,
        "tipo": tipo,
        "sharkscope_link": sharkscope_link,
    }
    selected_type = tipo.strip().lower()
    if selected_type not in {"jogador", "apoiador"}:
        form_data["tipo"] = "jogador"
        return render_register(request, "Tipo de usuário inválido.", form_data)

    normalized_email = email.strip().lower()
    normalized_doc = normalize_document(cpf_cnpj)
    normalized_phone = normalize_phone(telefone)
    form_data["email"] = normalized_email
    form_data["cpf_cnpj"] = normalized_doc
    form_data["telefone"] = format_phone(normalized_phone)
    form_data["tipo"] = selected_type

    if not EMAIL_PATTERN.match(normalized_email):
        return render_register(request, "Informe um email válido.", form_data)
    if not is_valid_cpf_cnpj(normalized_doc):
        return render_register(request, "CPF/CNPJ inválido.", form_data)
    if len(normalized_phone) not in {10, 11}:
        return render_register(request, "Telefone inválido. Informe DDD + número.", form_data)
    if not is_strong_password(senha):
        return render_register(request, "A senha deve ter 8+ caracteres, letras e números.", form_data)
    if senha != confirmar_senha:
        return render_register(request, "A confirmação da senha não confere.", form_data)

    exists_stmt = select(User).where(or_(User.email == normalized_email, User.cpf_cnpj == normalized_doc))
    existing_user = db.execute(exists_stmt).scalars().first()
    if existing_user:
        if existing_user.email == normalized_email:
            error = "Este email já está cadastrado."
        else:
            error = "Este CPF/CNPJ já está cadastrado."
        return render_register(request, error, form_data)

    cleanup_pending_registrations()
    code = f"{secrets.randbelow(1_000_000):06d}"
    token = str(uuid4())
    pending = {
        "token": token,
        "nome": nome.strip(),
        "email": normalized_email,
        "password_hash": get_password_hash(senha),
        "tipo": selected_type,
        "cpf_cnpj": normalized_doc,
        "telefone": normalized_phone,
        "sharkscope_link": sharkscope_link.strip() if selected_type == "jogador" and sharkscope_link else None,
        "endereco_completo": None,
        "data_nascimento": None,
        "bio": None,
        "verification_code": code,
        "attempts": 0,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=REGISTER_CODE_TTL_MINUTES),
    }
    PENDING_REGISTRATIONS[token] = pending
    request.session["pending_register_token"] = token

    email_sent = send_verification_code_email(normalized_email, code)
    if email_sent:
        return RedirectResponse(url="/register/verify", status_code=303)

    allow_fallback = os.getenv("ALLOW_LOCAL_EMAIL_FALLBACK", "true").strip().lower() in {"1", "true", "yes", "on"}
    if not allow_fallback:
        PENDING_REGISTRATIONS.pop(token, None)
        request.session.pop("pending_register_token", None)
        return render_register(request, "Não foi possível enviar o email de verificação.", form_data)

    return render_register_verify(
        request,
        pending,
        info=f"Modo teste: use o código {code} para validar seu cadastro.",
    )


@router.get("/register/verify", response_class=HTMLResponse)
def register_verify_form(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=303)
    pending = get_pending_registration_from_session(request)
    if not pending:
        return RedirectResponse(url="/register", status_code=303)
    return render_register_verify(request, pending)


@router.post("/register/verify", response_class=HTMLResponse)
def register_verify_submit(
    request: Request,
    code: str = Form(...),
    db: Session = Depends(get_db),
):
    pending = get_pending_registration_from_session(request)
    if not pending:
        return RedirectResponse(url="/register", status_code=303)

    code_digits = "".join(char for char in code if char.isdigit())
    if len(code_digits) != 6:
        return render_register_verify(request, pending, error="Informe o código de 6 dígitos.")

    if pending["attempts"] >= REGISTER_MAX_ATTEMPTS:
        PENDING_REGISTRATIONS.pop(pending["token"], None)
        request.session.pop("pending_register_token", None)
        return render_register(request, "Muitas tentativas. Refaça seu cadastro.")

    if code_digits != pending["verification_code"]:
        pending["attempts"] += 1
        attempts_left = REGISTER_MAX_ATTEMPTS - pending["attempts"]
        if attempts_left <= 0:
            PENDING_REGISTRATIONS.pop(pending["token"], None)
            request.session.pop("pending_register_token", None)
            return render_register(request, "Código inválido. Refaça seu cadastro.")
        return render_register_verify(
            request,
            pending,
            error=f"Código inválido. Restam {attempts_left} tentativa(s).",
        )

    exists_stmt = select(User).where(or_(User.email == pending["email"], User.cpf_cnpj == pending["cpf_cnpj"]))
    existing_user = db.execute(exists_stmt).scalars().first()
    if existing_user:
        PENDING_REGISTRATIONS.pop(pending["token"], None)
        request.session.pop("pending_register_token", None)
        return render_register(request, "Email ou CPF/CNPJ já foi cadastrado.")

    new_user = User(
        nome=pending["nome"],
        email=pending["email"],
        password_hash=pending["password_hash"],
        tipo=pending["tipo"],
        cpf_cnpj=pending["cpf_cnpj"],
        telefone=pending["telefone"],
        sharkscope_link=pending["sharkscope_link"],
        endereco_completo=pending["endereco_completo"],
        data_nascimento=pending["data_nascimento"],
        bio=pending["bio"],
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

    PENDING_REGISTRATIONS.pop(pending["token"], None)
    request.session.pop("pending_register_token", None)
    return RedirectResponse(url="/login?registered=1", status_code=303)


@router.post("/register/resend-code", response_class=HTMLResponse)
def register_resend_code(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=303)
    pending = get_pending_registration_from_session(request)
    if not pending:
        return RedirectResponse(url="/register", status_code=303)

    pending["verification_code"] = f"{secrets.randbelow(1_000_000):06d}"
    pending["attempts"] = 0
    pending["expires_at"] = datetime.now(timezone.utc) + timedelta(minutes=REGISTER_CODE_TTL_MINUTES)
    sent = send_verification_code_email(pending["email"], pending["verification_code"])
    if sent:
        return render_register_verify(request, pending, info="Enviamos um novo código para seu email.")

    allow_fallback = os.getenv("ALLOW_LOCAL_EMAIL_FALLBACK", "true").strip().lower() in {"1", "true", "yes", "on"}
    if allow_fallback:
        return render_register_verify(
            request,
            pending,
            info=f"Modo teste: novo código {pending['verification_code']}.",
        )
    return render_register_verify(request, pending, error="Não foi possível reenviar o código.")


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)
