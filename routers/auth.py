from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from db import get_db
from models import User

templates = Jinja2Templates(directory="templates")
router = APIRouter()

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def fetch_current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    stmt = select(User).where(User.id == user_id).options(joinedload(User.wallet))
    return db.execute(stmt).scalars().first()


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "user": user},
    )


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    senha: str = Form(...),
    db: Session = Depends(get_db),
):
    stmt = select(User).where(User.email == email)
    user = db.execute(stmt).scalars().first()
    if not user or not verify_password(senha, user.password_hash):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Credenciais inválidas."},
            status_code=401,
        )

    request.session["user_id"] = user.id
    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)
