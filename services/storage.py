import os
import secrets
from pathlib import Path

from fastapi import UploadFile

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
UPLOADS_DIR = STATIC_DIR / "uploads"
TMP_DIR = UPLOADS_DIR / "tmp"
KYC_DIR = UPLOADS_DIR / "kyc"
RESULTS_DIR = UPLOADS_DIR / "results"
PROFILE_DIR = UPLOADS_DIR / "profile"
MAX_UPLOAD_SIZE_BYTES = 8 * 1024 * 1024  # 8 MB
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_DOCUMENT_EXTENSIONS = ALLOWED_IMAGE_EXTENSIONS | {".pdf"}


def _ensure_directories() -> None:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    KYC_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)


def _get_extension(filename: str) -> str:
    _, ext = os.path.splitext(filename or "")
    return ext.lower()


def _safe_relative_path(path: Path) -> str:
    relative = path.relative_to(STATIC_DIR).as_posix()
    return f"/static/{relative}"


async def save_upload_to_temp(upload: UploadFile, *, kind: str) -> str:
    if not upload or not upload.filename:
        raise ValueError("Arquivo não enviado.")

    ext = _get_extension(upload.filename)
    allowed = ALLOWED_DOCUMENT_EXTENSIONS if kind == "document" else ALLOWED_IMAGE_EXTENSIONS
    if ext not in allowed:
        raise ValueError("Formato de arquivo não suportado.")

    _ensure_directories()
    filename = f"{kind}_{secrets.token_hex(16)}{ext}"
    destination = TMP_DIR / filename
    content = await upload.read()
    if not content:
        raise ValueError("Arquivo vazio.")
    if len(content) > MAX_UPLOAD_SIZE_BYTES:
        raise ValueError("Arquivo excede o limite de 8MB.")
    destination.write_bytes(content)
    await upload.close()
    return _safe_relative_path(destination)


def move_temp_file_to_kyc(temp_static_url: str, *, user_id: int, kind: str) -> str:
    if not temp_static_url.startswith("/static/uploads/tmp/"):
        raise ValueError("Arquivo temporário inválido.")

    _ensure_directories()
    source = BASE_DIR / temp_static_url.lstrip("/")
    if not source.exists():
        raise ValueError("Arquivo temporário não encontrado.")

    ext = source.suffix.lower()
    filename = f"user_{user_id}_{kind}_{secrets.token_hex(12)}{ext}"
    destination = KYC_DIR / filename
    source.replace(destination)
    return _safe_relative_path(destination)


async def save_match_file(upload: UploadFile, *, user_id: int, kind: str) -> str:
    if not upload or not upload.filename:
        raise ValueError("Arquivo não enviado.")

    ext = _get_extension(upload.filename)
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise ValueError("Formato de arquivo não suportado.")

    _ensure_directories()
    content = await upload.read()
    if not content:
        raise ValueError("Arquivo vazio.")
    if len(content) > MAX_UPLOAD_SIZE_BYTES:
        raise ValueError("Arquivo excede o limite de 8MB.")
    filename = f"user_{user_id}_{kind}_{secrets.token_hex(12)}{ext}"
    destination = RESULTS_DIR / filename
    destination.write_bytes(content)
    await upload.close()
    return _safe_relative_path(destination)


async def save_profile_photo(upload: UploadFile, *, user_id: int) -> str:
    if not upload or not upload.filename:
        raise ValueError("Arquivo não enviado.")
    ext = _get_extension(upload.filename)
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("Use uma imagem (JPG, PNG ou WebP).")
    _ensure_directories()
    content = await upload.read()
    if not content:
        raise ValueError("Arquivo vazio.")
    if len(content) > MAX_UPLOAD_SIZE_BYTES:
        raise ValueError("Arquivo excede o limite de 8MB.")
    filename = f"user_{user_id}_profile_{secrets.token_hex(8)}{ext}"
    destination = PROFILE_DIR / filename
    destination.write_bytes(content)
    await upload.close()
    return _safe_relative_path(destination)
