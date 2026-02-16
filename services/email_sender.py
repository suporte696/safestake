import os
import smtplib
import logging
from email.message import EmailMessage

import httpx

logger = logging.getLogger(__name__)


def _to_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def send_verification_code_email(to_email: str, code: str) -> bool:
    if _send_with_mailtrap_api(to_email, code):
        return True
    return _send_with_smtp(to_email, code)


def _send_with_mailtrap_api(to_email: str, code: str) -> bool:
    api_token = os.getenv("MAILTRAP_API_TOKEN")
    sender_email = os.getenv("MAILTRAP_SENDER_EMAIL", os.getenv("SMTP_FROM", "hello@demomailtrap.com"))
    sender_name = os.getenv("MAILTRAP_SENDER_NAME", "Safe Stake")
    category = os.getenv("MAILTRAP_CATEGORY", "Auth Verification")

    if not api_token:
        return False

    payload = {
        "from": {"email": sender_email, "name": sender_name},
        "to": [{"email": to_email}],
        "subject": "Safe Stake - Codigo de verificacao",
        "text": (
            "Seu codigo de verificacao Safe Stake e: "
            f"{code}\n\n"
            "Esse codigo expira em 10 minutos."
        ),
        "html": (
            "<html><body style='background:#050505;color:#ffffff;font-family:Arial,sans-serif;padding:24px;'>"
            "<h2 style='color:#00e560;'>Safe Stake</h2>"
            "<p>Use o codigo abaixo para concluir seu cadastro:</p>"
            f"<p style='font-size:28px;letter-spacing:8px;font-weight:bold;color:#00e560;'>{code}</p>"
            "<p>Esse codigo expira em 10 minutos.</p>"
            "</body></html>"
        ),
        "category": category,
    }

    try:
        response = httpx.post(
            "https://send.api.mailtrap.io/api/send",
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10.0,
        )
        if response.status_code in {200, 201, 202}:
            return True
        logger.warning(
            "Mailtrap API falhou: status=%s body=%s",
            response.status_code,
            response.text[:500],
        )
        return False
    except Exception as exc:
        logger.exception("Erro ao enviar email pela Mailtrap API: %s", exc)
        return False


def _send_with_smtp(to_email: str, code: str) -> bool:
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port_raw = os.getenv("SMTP_PORT", "587")
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    smtp_from = os.getenv("SMTP_FROM", "noreply@safestake.local")
    use_tls = _to_bool(os.getenv("SMTP_USE_TLS"), default=True)
    use_ssl = _to_bool(os.getenv("SMTP_USE_SSL"), default=False)

    if not smtp_host:
        return False

    try:
        smtp_port = int(smtp_port_raw)
    except ValueError:
        smtp_port = 587

    message = EmailMessage()
    message["Subject"] = "Safe Stake - Codigo de verificacao"
    message["From"] = smtp_from
    message["To"] = to_email
    message.set_content(
        (
            "Seu codigo de verificacao Safe Stake e: "
            f"{code}\n\n"
            "Esse codigo expira em 10 minutos."
        )
    )
    message.add_alternative(
        f"""
        <html>
          <body style="background:#050505;color:#ffffff;font-family:Arial,sans-serif;padding:24px;">
            <h2 style="color:#00e560;">Safe Stake</h2>
            <p>Use o codigo abaixo para concluir seu cadastro:</p>
            <p style="font-size:28px;letter-spacing:8px;font-weight:bold;color:#00e560;">{code}</p>
            <p>Esse codigo expira em 10 minutos.</p>
          </body>
        </html>
        """,
        subtype="html",
    )

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10) as smtp:
                if smtp_user and smtp_password:
                    smtp.login(smtp_user, smtp_password)
                smtp.send_message(message)
            return True

        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
            smtp.ehlo()
            if use_tls:
                smtp.starttls()
                smtp.ehlo()
            if smtp_user and smtp_password:
                smtp.login(smtp_user, smtp_password)
            smtp.send_message(message)
        return True
    except Exception as exc:
        logger.exception("Erro ao enviar email por SMTP: %s", exc)
        return False
