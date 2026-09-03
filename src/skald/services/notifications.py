import json
import logging
import re
import smtplib
import ssl
from collections.abc import Callable
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from urllib.parse import quote, quote_plus, unquote, unquote_plus
from urllib.request import Request, urlopen

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from skald.config import Settings
from skald.models import (
    DeliveryOutcome,
    NotificationChannel,
    NotificationDeliveryAttempt,
    SubscriptionEvent,
)

NOTIFICATION_TIMEOUT_SECONDS = 10.0

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NotificationDeliveryService:
    """Best-effort delivery for events that have already been committed."""

    def __init__(self, session_factory: Callable[[], Session], settings: Settings) -> None:
        self._session_factory = session_factory
        self._settings = settings

    def deliver_event(self, event_id: int) -> None:
        for channel in (NotificationChannel.TELEGRAM, NotificationChannel.EMAIL):
            try:
                self._deliver_channel(event_id, channel)
            except Exception:  # noqa: BLE001 - channels must be independently best-effort
                # Database/audit failures must not prevent the other channel,
                # and their exception text is deliberately not logged because
                # provider exceptions may contain credentials or recipients.
                continue

    def _deliver_channel(self, event_id: int, channel: NotificationChannel) -> None:
        """Reserve, deliver or skip, then persist one channel's final outcome."""
        with self._session_factory() as session:
            event = session.get(SubscriptionEvent, event_id)
            if event is None:
                logger.warning("notification event %s no longer exists", event_id)
                return
            attempt_id = _reserve_attempt(session, event_id, channel)
            if attempt_id is None:
                return
            subject = f"Skald: {event.title}"[:200]
            body = f"{event.body}\n\nOpen Skald to review this event."[:1000]

        if not _channel_is_configured(channel, self._settings):
            return

        try:
            message_id = (
                send_telegram(self._settings, subject, body)
                if channel is NotificationChannel.TELEGRAM
                else send_email(self._settings, subject, body)
            )
        except Exception as exc:  # noqa: BLE001 - provider failures are audit outcomes
            logger.error(
                "notification delivery failed event=%s channel=%s error_type=%s",
                event_id,
                channel.value,
                type(exc).__name__,
            )
            _finish_attempt(
                self._session_factory,
                attempt_id,
                outcome=DeliveryOutcome.FAILED,
                error_summary=sanitize_delivery_error(exc, self._settings),
            )
            return

        _finish_attempt(
            self._session_factory,
            attempt_id,
            outcome=DeliveryOutcome.SENT,
            provider_message_id=message_id[:128] if message_id else None,
        )


def _reserve_attempt(session: Session, event_id: int, channel: NotificationChannel) -> int | None:
    """Durably reserve a channel before any provider I/O."""
    try:
        with session.begin_nested():
            attempt = NotificationDeliveryAttempt(
                event_id=event_id,
                channel=channel,
                outcome=DeliveryOutcome.SKIPPED,
                attempted_at=_utcnow(),
            )
            session.add(attempt)
            session.flush()
    except IntegrityError:
        return None
    session.commit()
    if attempt.id is None:
        raise RuntimeError("notification attempt reservation did not receive an ID")
    return attempt.id


def _finish_attempt(
    session_factory: Callable[[], Session],
    attempt_id: int,
    *,
    outcome: DeliveryOutcome,
    provider_message_id: str | None = None,
    error_summary: str | None = None,
) -> None:
    """Update the reservation in a fresh, short-lived database session."""
    with session_factory() as session:
        attempt = session.get(NotificationDeliveryAttempt, attempt_id)
        if attempt is None:
            return
        attempt.outcome = outcome
        attempt.provider_message_id = provider_message_id
        attempt.error_summary = error_summary
        session.add(attempt)
        session.commit()


def _channel_is_configured(channel: NotificationChannel, settings: Settings) -> bool:
    if channel is NotificationChannel.TELEGRAM:
        return bool(settings.telegram_bot_token and settings.telegram_chat_id)
    return bool(settings.smtp_host and settings.smtp_from and settings.smtp_to)


def send_telegram(settings: Settings, subject: str, body: str) -> str | None:
    """Send one plain-text Bot API message and return a supplied message ID."""
    url = "https://api.telegram.org/bot" + quote(settings.telegram_bot_token, safe="") + "/sendMessage"
    payload = json.dumps(
        {"chat_id": settings.telegram_chat_id, "text": f"{subject}\n\n{body}"}
    ).encode("utf-8")
    request = Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=NOTIFICATION_TIMEOUT_SECONDS) as response:
        try:
            response_data = json.loads(response.read().decode("utf-8"))
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            raise RuntimeError("Telegram rejected notification") from None
    if not isinstance(response_data, dict) or response_data.get("ok") is not True:
        raise RuntimeError("Telegram rejected notification")
    result = response_data.get("result")
    if not isinstance(result, dict) or "message_id" not in result:
        return None
    return str(result["message_id"])


def send_email(settings: Settings, subject: str, body: str) -> str | None:
    """Send one plain-text SMTP message; SMTP does not expose a message ID."""
    sender = settings.smtp_from.strip()
    recipient = settings.smtp_to.strip()
    parsed_sender = parseaddr(sender)[1]
    parsed_recipient = parseaddr(recipient)[1]
    if (
        "@" not in parsed_sender
        or "@" not in parsed_recipient
        or parsed_sender != sender
        or parsed_recipient != recipient
    ):
        raise ValueError("SMTP sender or recipient is invalid")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            context=context,
            timeout=NOTIFICATION_TIMEOUT_SECONDS,
        ) as client:
            _send_smtp_message(client, settings, message)
    else:
        with smtplib.SMTP(
            settings.smtp_host, settings.smtp_port, timeout=NOTIFICATION_TIMEOUT_SECONDS
        ) as client:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
            _send_smtp_message(client, settings, message)
    return None


def _send_smtp_message(client, settings: Settings, message: EmailMessage) -> None:
    if settings.smtp_username:
        client.login(settings.smtp_username, settings.smtp_password)
    client.send_message(message)


def sanitize_delivery_error(exc: Exception, settings: Settings) -> str:
    """Produce a bounded diagnostic without configured credentials or addresses."""
    message = " ".join(str(exc).split())
    sensitive_values = (
        settings.telegram_bot_token,
        settings.telegram_chat_id,
        settings.smtp_username,
        settings.smtp_password,
        settings.smtp_from,
        settings.smtp_to,
    )
    variants: set[str] = set()
    for value in sensitive_values:
        if not value:
            continue
        for raw_value in (value, value.strip()):
            if raw_value:
                variants.update(
                    (raw_value, quote(raw_value, safe=""), quote_plus(raw_value, safe=""))
                )
    for variant in sorted(variants, key=len, reverse=True):
        message = message.replace(variant, "[redacted]")
    # Decode percent-encoded forms that differ only by hexadecimal case, then
    # repeat exact matching before generic filtering removes punctuation.
    message = unquote_plus(unquote(message))
    for variant in sorted(variants, key=len, reverse=True):
        message = message.replace(variant, "[redacted]")

    message = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", "[redacted]", message)
    cleaned = "".join(char for char in message if char.isalnum() or char in " .:_-")
    prefix = type(exc).__name__
    return (f"{prefix}: {cleaned}" if cleaned.strip() else f"{prefix}: delivery failed")[:200]
