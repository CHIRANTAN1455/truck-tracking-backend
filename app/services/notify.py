"""Customer / ops notifications and share-link helpers.

Every message is stored in the `notifications` table. Email goes out over SMTP when SMTP_HOST is
configured; SMS and WhatsApp are logged until a provider (Twilio, Gupshup, Meta Cloud API, ...) is
plugged into `_send_sms` / `_send_whatsapp`.
"""

import logging
import smtplib
from email.message import EmailMessage
from urllib.parse import quote

from sqlalchemy.orm import Session

from ..config import settings
from ..models import Notification

log = logging.getLogger(__name__)


def tracking_url(token: str) -> str:
    return f"{settings.public_tracking_base_url.rstrip('/')}/?track={token}"


def share_links(token: str, phone: str | None, email: str | None, shipment_id: str) -> dict:
    url = tracking_url(token)
    text = f"Track your shipment {shipment_id} live: {url}"
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return {
        "url": url,
        "whatsapp": f"https://wa.me/{digits}?text={quote(text)}" if digits else f"https://wa.me/?text={quote(text)}",
        "sms": f"sms:{phone or ''}?body={quote(text)}",
        "email": f"mailto:{email or ''}?subject={quote('Shipment ' + shipment_id)}&body={quote(text)}",
    }


def _send_email(to: str, subject: str, body: str) -> str:
    if not settings.smtp_host:
        return "logged"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.smtp_from, to, subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as s:
            s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
        return "sent"
    except Exception as exc:
        log.warning("Email to %s failed: %s", to, exc)
        return "failed"


def _send_sms(to: str, body: str) -> str:
    return "logged"


def _send_whatsapp(to: str, body: str) -> str:
    return "logged"


def send(db: Session, channel: str, recipient: str | None, message: str, trip_id: str | None = None,
         subject: str = "Shipment update") -> None:
    if channel == "email" and recipient:
        status = _send_email(recipient, subject, message)
    elif channel == "sms" and recipient:
        status = _send_sms(recipient, message)
    elif channel == "whatsapp" and recipient:
        status = _send_whatsapp(recipient, message)
    else:
        status = "logged"
    log.info("[notify:%s] %s -> %s: %s", status, channel, recipient, message)
    db.add(Notification(trip_id=trip_id, channel=channel, recipient=recipient, message=message, status=status))


def notify_customer(db: Session, shipment, message: str, trip_id: str | None) -> None:
    if shipment.customer_phone:
        send(db, "sms", shipment.customer_phone, message, trip_id)
        send(db, "whatsapp", shipment.customer_phone, message, trip_id)
    if shipment.customer_email:
        send(db, "email", shipment.customer_email, message, trip_id, subject=f"Shipment {shipment.id}")


def notify_ops(db: Session, message: str, trip_id: str | None) -> None:
    send(db, "ops", "operations", message, trip_id)
