import logging

from relay.app import configure_logging


def test_httpx_loggers_never_log_request_urls() -> None:
    """Токен бота — часть URL Telegram; INFO у httpx выложил бы его в лог каждым запросом."""
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)

    configure_logging("DEBUG")

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
