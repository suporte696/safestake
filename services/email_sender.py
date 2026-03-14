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


def _build_base_email_html(title: str, subtitle: str, content_html: str) -> str:
    return f"""
    <html>
      <body style='background-color:#000000; color:#ffffff; font-family:"Inter", sans-serif; padding:40px 20px; text-align:center;'>
        <div style='max-width:480px; margin:0 auto; background-color:#0a0a0a; border:1px solid rgba(255,255,255,0.1); border-radius:24px; padding:40px;'>
          <div style='margin-bottom:32px;'>
            <img src="https://safestakeaa.com/static/img/safestake-text.png" alt="SAFE STAKE" style="height:32px; margin:0 auto;" />
          </div>
          <h1 style='font-size:24px; font-weight:700; color:#ffffff; margin-bottom:16px; text-transform:uppercase; letter-spacing:1px;'>{title}</h1>
          <p style='color:#a1a1aa; font-size:16px; margin-bottom:32px;'>{subtitle}</p>
          
          {content_html}
          
          <div style='margin-top:40px; padding-top:24px; border-top:1px solid rgba(255,255,255,0.05); color:#52525b; font-size:12px;'>
            <p>© 2026 SAFE STAKE. Todos os direitos reservados.</p>
          </div>
        </div>
      </body>
    </html>
    """


def send_verification_code_email(to_email: str, code: str) -> bool:
    subject = "Safe Stake - Verifique seu email de cadastro"
    text = (
        "Seu código de verificação Safe Stake é: "
        f"{code}\n\n"
        "Esse código expira em 10 minutos."
    )
    
    content_html = f"""
        <div style='background-color:rgba(16,185,129,0.1); border:1px solid rgba(16,185,129,0.2); border-radius:16px; padding:24px; margin-bottom:32px;'>
          <span style='font-family:monospace; font-size:42px; font-weight:700; color:#34d399; letter-spacing:8px;'>{code}</span>
        </div>
        <p style='color:#71717a; font-size:14px;'>Esse código expira em <b>10 minutos</b>.</p>
    """
    html = _build_base_email_html(
        title="Verifique seu Email",
        subtitle="Use o código abaixo para concluir seu cadastro na plataforma.",
        content_html=content_html
    )
    
    if _send_with_mailtrap_api(to_email, subject, text, html):
        return True
    return _send_with_smtp(to_email, subject, text, html)


def send_withdrawal_approved_email(to_email: str, amount: float) -> bool:
    subject = "Safe Stake - Saque Aprovado"
    text = (
        "Seu saque foi aprovado!\n\n"
        f"O valor de US$ {amount:.2f} foi enviado para sua conta via PIX."
    )
    
    content_html = f"""
        <div style='background-color:rgba(16,185,129,0.1); border:1px solid rgba(16,185,129,0.2); border-radius:16px; padding:24px; margin-bottom:32px;'>
          <span style='font-family:monospace; font-size:42px; font-weight:700; color:#34d399;'>US$ {amount:.2f}</span>
        </div>
        <p style='color:#71717a; font-size:14px;'>A transferência PIX já foi realizada e deve refletir na sua conta bancária em instantes.</p>
    """
    html = _build_base_email_html(
        title="Saque Aprovado",
        subtitle="Excelente notícia! Seu saque acaba de ser processado.",
        content_html=content_html
    )
    if _send_with_mailtrap_api(to_email, subject, text, html):
        return True
    return _send_with_smtp(to_email, subject, text, html)


def send_match_started_email(to_email: str, tournament_name: str, player_name: str) -> bool:
    subject = f"Safe Stake - A partida vai começar: {tournament_name}!"
    text = (
        f"O jogador {player_name} acaba de registrar que começou a jogar o torneio {tournament_name} que você apoiou.\n\n"
        "Sorte nas mesas!"
    )
    
    content_html = f"""
        <div style='background-color:rgba(59,130,246,0.1); border:1px solid rgba(59,130,246,0.2); border-radius:16px; padding:24px; margin-bottom:32px;'>
          <p style='font-family:monospace; font-size:18px; font-weight:700; color:#60a5fa; margin:0;'>{tournament_name}</p>
          <p style='font-size:14px; color:#94a3b8; margin-top:8px; margin-bottom:0;'>Jogado por {player_name}</p>
        </div>
        <p style='color:#71717a; font-size:14px;'>A partida iniciou oficialmente. Boa sorte ao jogador e aos apoiadores!</p>
    """
    html = _build_base_email_html(
        title="Partida Iniciada",
        subtitle="A ação já começou nas mesas!",
        content_html=content_html
    )
    if _send_with_mailtrap_api(to_email, subject, text, html):
        return True
    return _send_with_smtp(to_email, subject, text, html)


def send_match_ended_email(to_email: str, tournament_name: str, player_name: str, result_amount: float) -> bool:
    is_itm = result_amount > 0
    bg_color = "rgba(16,185,129,0.1)" if is_itm else "rgba(107,114,128,0.1)"
    border_color = "rgba(16,185,129,0.2)" if is_itm else "rgba(107,114,128,0.2)"
    text_color = "#34d399" if is_itm else "#9ca3af"
    result_text = f"US$ {result_amount:.2f}" if is_itm else "Sem premiação"
    
    subject = f"Safe Stake - Resultado da partida: {tournament_name}"
    text = (
        f"A partida {tournament_name} jogada por {player_name} foi encerrada.\n\n"
        f"Prêmio total retornado: {result_text}\n\n"
        "O saldo proporcional ao seu apoio foi creditado/atualizado na sua carteira."
    )
    
    content_html = f"""
        <div style='background-color:{bg_color}; border:1px solid {border_color}; border-radius:16px; padding:24px; margin-bottom:32px;'>
          <p style='font-family:monospace; font-size:18px; font-weight:700; color:#e2e8f0; margin:0; margin-bottom:12px;'>{tournament_name}</p>
          <span style='font-family:monospace; font-size:32px; font-weight:700; color:{text_color};'>{result_text}</span>
        </div>
        <p style='color:#71717a; font-size:14px;'>O jogador reportou o encerramento do torneio e o saldo proporcional (se houver lucro) já foi direcionado para sua conta no Safe Stake.</p>
    """
    html = _build_base_email_html(
        title="Partida Finalizada",
        subtitle=f"Resultado da oferta que você apoiou de {player_name}.",
        content_html=content_html
    )
    if _send_with_mailtrap_api(to_email, subject, text, html):
        return True
    return _send_with_smtp(to_email, subject, text, html)


def send_password_changed_email(to_email: str) -> bool:
    subject = "Safe Stake - Sua senha foi alterada"
    text = (
        "Notamos que a senha da sua conta no Safe Stake foi alterada recentemente.\n\n"
        "Se foi você, nenhuma ação é necessária.\nCaso contrário, contate o suporte imediatamente."
    )
    
    content_html = f"""
        <div style='background-color:rgba(245,158,11,0.1); border:1px solid rgba(245,158,11,0.2); border-radius:16px; padding:24px; margin-bottom:32px;'>
          <p style='font-size:16px; font-weight:600; color:#fbbf24; margin:0;'>Senha Atualizada</p>
        </div>
        <p style='color:#71717a; font-size:14px;'>Se você realizou essa alteração, pode ignorar este e-mail.</p>
        <p style='color:#71717a; font-size:14px;'>Mas se não foi você, recomendamos entrar em contato com o suporte imediatamente para proteger sua conta.</p>
    """
    html = _build_base_email_html(
        title="Alerta de Segurança",
        subtitle="Houve uma alteração na sua conta.",
        content_html=content_html
    )
    if _send_with_mailtrap_api(to_email, subject, text, html):
        return True
    return _send_with_smtp(to_email, subject, text, html)


def _send_with_mailtrap_api(to_email: str, subject: str, text: str, html: str) -> bool:
    api_token = os.getenv("MAILTRAP_API_TOKEN")
    sender_email = os.getenv("MAILTRAP_SENDER_EMAIL", os.getenv("SMTP_FROM", "hello@demomailtrap.com"))
    sender_name = os.getenv("MAILTRAP_SENDER_NAME", "Safe Stake")
    category = os.getenv("MAILTRAP_CATEGORY", "Transactional")

    if not api_token:
        return False

    payload = {
        "from": {"email": sender_email, "name": sender_name},
        "to": [{"email": to_email}],
        "subject": subject,
        "text": text,
        "html": html,
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


def _send_with_smtp(to_email: str, subject: str, text: str, html: str) -> bool:
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
    message["Subject"] = subject
    message["From"] = smtp_from
    message["To"] = to_email
    message.set_content(text)
    message.add_alternative(html, subtype="html")

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
