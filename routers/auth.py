from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request, Security
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import APIKeyCookie
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from db import get_db
from models import User, Wallet

templates = Jinja2Templates(directory="templates")
router = APIRouter()

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
session_cookie = APIKeyCookie(name="safe_stake_session", auto_error=False)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def normalize_document(value: str) -> str:
    return "".join(char for char in value if char.isdigit())


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
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "user": user, "wallet": get_wallet_summary(user)},
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
    return templates.TemplateResponse(
        "register.html",
        {"request": request, "user": None, "wallet": None, "form_data": {"tipo": "jogador"}},
    )


@router.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    nome: str = Form(...),
    email: str = Form(...),
    cpf_cnpj: str = Form(...),
    telefone: str = Form(...),
    senha: str = Form(...),
    tipo: str = Form(...),
    sharkscope_link: str = Form(""),
    db: Session = Depends(get_db),
):
    selected_type = tipo.strip().lower()
    if selected_type not in {"jogador", "apoiador"}:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "error": "Tipo de usuário inválido.",
                "user": None,
                "wallet": None,
                "form_data": {
                    "nome": nome,
                    "email": email,
                    "cpf_cnpj": cpf_cnpj,
                    "telefone": telefone,
                    "tipo": "jogador",
                    "sharkscope_link": sharkscope_link,
                },
            },
            status_code=400,
        )

    normalized_email = email.strip().lower()
    normalized_doc = normalize_document(cpf_cnpj)
    if not normalized_email or len(senha) < 6:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "error": "Preencha os campos corretamente (senha mínima de 6 caracteres).",
                "user": None,
                "wallet": None,
                "form_data": {
                    "nome": nome,
                    "email": normalized_email,
                    "cpf_cnpj": normalized_doc,
                    "telefone": telefone,
                    "tipo": selected_type,
                    "sharkscope_link": sharkscope_link,
                },
            },
            status_code=400,
        )
    if len(normalized_doc) < 11 or len(normalized_doc) > 14:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "error": "CPF/CNPJ deve conter entre 11 e 14 dígitos.",
                "user": None,
                "wallet": None,
                "form_data": {
                    "nome": nome,
                    "email": normalized_email,
                    "cpf_cnpj": normalized_doc,
                    "telefone": telefone,
                    "tipo": selected_type,
                    "sharkscope_link": sharkscope_link,
                },
            },
            status_code=400,
        )

    exists_stmt = select(User).where(or_(User.email == normalized_email, User.cpf_cnpj == normalized_doc))
    existing_user = db.execute(exists_stmt).scalars().first()
    if existing_user:
        if existing_user.email == normalized_email:
            error = "Este email já está cadastrado."
        else:
            error = "Este CPF/CNPJ já está cadastrado."
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "error": error,
                "user": None,
                "wallet": None,
                "form_data": {
                    "nome": nome,
                    "email": normalized_email,
                    "cpf_cnpj": normalized_doc,
                    "telefone": telefone,
                    "tipo": selected_type,
                    "sharkscope_link": sharkscope_link,
                },
            },
            status_code=400,
        )

    new_user = User(
        nome=nome.strip(),
        email=normalized_email,
        password_hash=get_password_hash(senha),
        tipo=selected_type,
        cpf_cnpj=normalized_doc,
        telefone=telefone.strip(),
        sharkscope_link=sharkscope_link.strip() if selected_type == "jogador" and sharkscope_link else None,
        endereco_completo=None,
        data_nascimento=None,
        bio=None,
        is_verified=False,
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
    return RedirectResponse(url="/login", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)
