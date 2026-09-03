import json
import smtplib
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import SQLModel, Session, select

from skald.config import Settings
from skald.db import get_engine
from skald.models import (
    DeliveryOutcome,
    MediaSubscription,
    MediaType,
    NotificationChannel,
    NotificationDeliveryAttempt,
    SubscriptionEvent,
    SubscriptionEventKind,
)
from skald.services.notifications import (
    NotificationDeliveryService,
    sanitize_delivery_error,
    send_email,
    send_telegram,
)


@pytest.fixture
def session_factory(tmp_path):
    engine = get_engine(str(tmp_path / "notifications.db"))
    SQLModel.metadata.create_all(engine)
    return lambda: Session(engine)


@pytest.fixture
def event(session_factory):
    with session_factory() as session:
        subscription = MediaSubscription(tmdb_id=1, type=MediaType.MOVIE, title="Film")
        session.add(subscription)
        session.commit()
        event = SubscriptionEvent(
            subscription_id=subscription.id,
            media_type=MediaType.MOVIE,
            kind=SubscriptionEventKind.RELEASE_MATCH,
            dedupe_key="release:notification-test",
            title="New release",
            body="Film is available",
        )
        session.add(event)
        session.commit()
        return event.id


def test_missing_channel_configuration_records_skipped_attempts(session_factory, event):
    NotificationDeliveryService(session_factory, Settings(_env_file=None)).deliver_event(event)

    with session_factory() as session:
        attempts = session.exec(
            select(NotificationDeliveryAttempt).order_by(NotificationDeliveryAttempt.channel)
        ).all()
    assert [(attempt.channel, attempt.outcome) for attempt in attempts] == [
        (NotificationChannel.EMAIL, DeliveryOutcome.SKIPPED),
        (NotificationChannel.TELEGRAM, DeliveryOutcome.SKIPPED),
    ]


def test_telegram_failure_is_audited_and_does_not_block_email(session_factory, event, monkeypatch):
    def fail_telegram(settings, subject, body):
        raise TimeoutError("secret-token recipient@example.test")

    monkeypatch.setattr("skald.services.notifications.send_telegram", fail_telegram)
    monkeypatch.setattr("skald.services.notifications.send_email", lambda *args: None)
    settings = Settings(
        _env_file=None,
        telegram_bot_token="secret-token",
        telegram_chat_id="chat-1",
        smtp_host="smtp.example.test",
        smtp_from="sender@example.test",
        smtp_to="recipient@example.test",
    )

    NotificationDeliveryService(session_factory, settings).deliver_event(event)

    with session_factory() as session:
        attempts = {
            attempt.channel: attempt
            for attempt in session.exec(select(NotificationDeliveryAttempt)).all()
        }
    assert attempts[NotificationChannel.TELEGRAM].outcome is DeliveryOutcome.FAILED
    assert attempts[NotificationChannel.EMAIL].outcome is DeliveryOutcome.SENT
    assert "secret-token" not in attempts[NotificationChannel.TELEGRAM].error_summary
    assert "recipient@example.test" not in attempts[NotificationChannel.TELEGRAM].error_summary


def test_existing_attempt_prevents_repeat_provider_call(session_factory, event, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "skald.services.notifications.send_telegram", lambda *args: calls.append("telegram")
    )
    service = NotificationDeliveryService(
        session_factory,
        Settings(_env_file=None, telegram_bot_token="token", telegram_chat_id="chat"),
    )

    service.deliver_event(event)
    service.deliver_event(event)

    assert calls == ["telegram"]


def test_send_telegram_rejects_malformed_provider_response(monkeypatch):
    class Response:
        def read(self):
            return json.dumps({"ok": False, "description": "do not persist this"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("skald.services.notifications.urlopen", lambda request, timeout: Response())

    with pytest.raises(RuntimeError, match="Telegram rejected notification"):
        send_telegram(Settings(_env_file=None, telegram_bot_token="token", telegram_chat_id="chat"), "s", "b")


def test_send_email_uses_short_lived_starttls_session(monkeypatch):
    calls = []

    class SMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            calls.append(("ehlo",))

        def starttls(self, context):
            calls.append(("starttls", context))

        def login(self, username, password):
            calls.append(("login", username, password))

        def send_message(self, message):
            calls.append(("send", message["To"], message.get_content().strip()))

    monkeypatch.setattr("skald.services.notifications.smtplib.SMTP", SMTP)
    settings = Settings(
        _env_file=None,
        smtp_host="smtp.example.test",
        smtp_username="user",
        smtp_password="password",
        smtp_from="sender@example.test",
        smtp_to="recipient@example.test",
    )

    assert send_email(settings, "Subject", "Body") is None
    assert [call[0] for call in calls] == ["connect", "ehlo", "starttls", "ehlo", "login", "send"]


def test_sanitize_delivery_error_redacts_raw_and_url_encoded_sensitive_values():
    settings = Settings(
        _env_file=None,
        telegram_bot_token="bot/token?x=1",
        telegram_chat_id="chat/with space",
        smtp_username="user name",
        smtp_password="pa:ss/word",
        smtp_from='"Ålice, Admin" <alice@example.test>',
        smtp_to='"Bøb" <bob@example.test>',
    )
    sensitive_values = (
        settings.telegram_bot_token,
        settings.telegram_chat_id,
        settings.smtp_username,
        settings.smtp_password,
        settings.smtp_from,
        settings.smtp_to,
    )
    message = " ".join(value + " " + quote(value, safe="") for value in sensitive_values)
    summary = sanitize_delivery_error(RuntimeError(message * 20), settings)

    assert len(summary) <= 200
    assert all(value not in summary and quote(value, safe="") not in summary for value in sensitive_values)
    assert "alice@example.test" not in summary
    assert "bob@example.test" not in summary


def test_sanitize_delivery_error_redacts_stripped_quoted_smtp_recipient():
    recipient = '  "ops team"@example.com  '
    settings = Settings(_env_file=None, smtp_to=recipient)
    rejected_address = recipient.strip()

    summary = sanitize_delivery_error(
        smtplib.SMTPRecipientsRefused({rejected_address: (550, b"rejected")}), settings
    )

    assert rejected_address not in summary
    assert "ops team" not in summary
    assert "example.com" not in summary
    assert len(summary) <= 200


def test_telegram_success_returns_provider_message_id(monkeypatch):
    captured = {}

    class Response:
        def read(self):
            return b'{"ok": true, "result": {"message_id": 42}}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def open_request(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("skald.services.notifications.urlopen", open_request)
    settings = Settings(_env_file=None, telegram_bot_token="token/a", telegram_chat_id="chat")

    assert send_telegram(settings, "subject", "body") == "42"
    assert captured == {
        "url": "https://api.telegram.org/bottoken%2Fa/sendMessage",
        "payload": {"chat_id": "chat", "text": "subject\n\nbody"},
        "timeout": 10.0,
    }


def test_delivery_audit_survives_event_deleted_during_provider_io(session_factory, event, monkeypatch):
    def delete_event_during_send(*args):
        with session_factory() as session:
            stored = session.get(SubscriptionEvent, event)
            session.delete(stored)
            session.commit()
        return "provider-id"

    monkeypatch.setattr("skald.services.notifications.send_telegram", delete_event_during_send)
    service = NotificationDeliveryService(
        session_factory,
        Settings(_env_file=None, telegram_bot_token="token", telegram_chat_id="chat"),
    )

    service.deliver_event(event)

    with session_factory() as session:
        attempt = session.exec(
            select(NotificationDeliveryAttempt).where(
                NotificationDeliveryAttempt.channel == NotificationChannel.TELEGRAM
            )
        ).one()
    assert (attempt.event_id, attempt.outcome, attempt.provider_message_id) == (
        None,
        DeliveryOutcome.SENT,
        "provider-id",
    )


def test_telegram_urlopen_timeout_propagates_without_response_details(monkeypatch):
    monkeypatch.setattr(
        "skald.services.notifications.urlopen",
        lambda request, timeout: (_ for _ in ()).throw(TimeoutError("provider timeout")),
    )
    settings = Settings(_env_file=None, telegram_bot_token="token", telegram_chat_id="chat")

    with pytest.raises(TimeoutError, match="provider timeout"):
        send_telegram(settings, "subject", "body")


def test_send_email_uses_ssl_for_implicit_tls(monkeypatch):
    calls = []

    class SMTPSSL:
        def __init__(self, host, port, *, context, timeout):
            calls.append(("connect", host, port, timeout, context))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, username, password):
            calls.append(("login", username, password))

        def send_message(self, message):
            calls.append(("send", message["To"]))

    monkeypatch.setattr("skald.services.notifications.smtplib.SMTP_SSL", SMTPSSL)
    settings = Settings(
        _env_file=None,
        smtp_host="smtp.example.test",
        smtp_port=465,
        smtp_username="user",
        smtp_password="password",
        smtp_from="sender@example.test",
        smtp_to="recipient@example.test",
    )

    send_email(settings, "Subject", "Body")
    assert [call[0] for call in calls] == ["connect", "login", "send"]


@pytest.mark.parametrize(
    ("failure_at", "exc"),
    [
        ("starttls", smtplib.SMTPException("TLS rejected recipient@example.test")),
        ("starttls", TimeoutError("TLS timeout")),
        ("send", smtplib.SMTPRecipientsRefused({"recipient@example.test": (550, b"rejected")})),
        ("send", RuntimeError("unexpected SMTP failure")),
    ],
)
def test_send_email_propagates_starttls_and_provider_failures(monkeypatch, failure_at, exc):
    class SMTP:
        def __init__(self, *args, **kwargs):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            return None

        def starttls(self, context):
            if failure_at == "starttls":
                raise exc

        def send_message(self, message):
            if failure_at == "send":
                raise exc

    monkeypatch.setattr("skald.services.notifications.smtplib.SMTP", SMTP)
    settings = Settings(
        _env_file=None,
        smtp_host="smtp.example.test",
        smtp_from="sender@example.test",
        smtp_to="recipient@example.test",
    )

    with pytest.raises(type(exc)):
        send_email(settings, "Subject", "Body")


def test_concurrent_attempt_reservations_contact_provider_once(session_factory, event, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "skald.services.notifications.send_telegram", lambda *args: calls.append("telegram")
    )
    service = NotificationDeliveryService(
        session_factory,
        Settings(_env_file=None, telegram_bot_token="token", telegram_chat_id="chat"),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: service.deliver_event(event), range(2)))

    with session_factory() as session:
        attempts = session.exec(
            select(NotificationDeliveryAttempt).where(
                NotificationDeliveryAttempt.channel == NotificationChannel.TELEGRAM
            )
        ).all()
    assert len(attempts) == 1
    assert calls == ["telegram"]


def test_finalization_failure_does_not_stop_the_next_channel(session_factory, event, monkeypatch):
    delivered = []
    monkeypatch.setattr(
        "skald.services.notifications.send_telegram", lambda *args: delivered.append("telegram")
    )
    monkeypatch.setattr(
        "skald.services.notifications.send_email", lambda *args: delivered.append("email"))
    original_finish = __import__("skald.services.notifications", fromlist=["_finish_attempt"])._finish_attempt

    failed_once = False

    def fail_telegram_finalization(session_factory_arg, attempt_id, **kwargs):
        nonlocal failed_once
        if kwargs["outcome"] is DeliveryOutcome.SENT and not failed_once:
            failed_once = True
            raise SQLAlchemyError("attempt finalization unavailable")
        return original_finish(session_factory_arg, attempt_id, **kwargs)

    monkeypatch.setattr("skald.services.notifications._finish_attempt", fail_telegram_finalization)
    settings = Settings(
        _env_file=None,
        telegram_bot_token="token",
        telegram_chat_id="chat",
        smtp_host="smtp.example.test",
        smtp_from="sender@example.test",
        smtp_to="recipient@example.test",
    )

    NotificationDeliveryService(session_factory, settings).deliver_event(event)

    assert delivered == ["telegram", "email"]
