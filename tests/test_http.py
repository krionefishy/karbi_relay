import re

import respx
from httpx import Response

from tests.conftest import TELEGRAM, auth_header

GET_ME = {
    "ok": True,
    "result": {"id": 42, "username": "turnover_alerts_bot", "first_name": "Оборачиваемость"},
}


def register(client, bot_code: str = "turnover-alerts", token: str = "111:AAA") -> dict:
    with respx.mock(assert_all_called=False) as router:
        router.post(re.compile(rf"{TELEGRAM}/bot.*/getMe")).mock(return_value=Response(200, json=GET_ME))
        response = client.post(
            "/api/v1/bots",
            json={"bot_code": bot_code, "action": "upsert", "token": token},
            headers=auth_header(),
        )
    assert response.status_code == 200, response.text
    return response.json()


class TestChannelAuth:
    def test_a_call_without_a_token_is_rejected(self, client) -> None:
        assert client.post("/api/v1/messages/send", json={}).status_code == 401

    def test_a_token_for_the_other_direction_is_rejected(self, client) -> None:
        # The main server's own audience must not open the relay's doors.
        response = client.get("/ready", headers=auth_header(audience="main"))
        assert response.status_code == 401

    def test_an_expired_token_is_rejected(self, client) -> None:
        assert client.get("/ready", headers=auth_header(expired=True)).status_code == 401

    def test_a_foreign_issuer_is_rejected(self, client) -> None:
        assert client.get("/ready", headers=auth_header(issuer="somebody-else")).status_code == 401

    def test_health_needs_no_token(self, client) -> None:
        assert client.get("/health").json() == {"status": "ok"}


class TestBotRegistration:
    def test_registration_returns_the_invite_link_template(self, client) -> None:
        body = register(client)
        assert body["invite_link_template"] == "https://t.me/turnover_alerts_bot?start={token}"
        assert body["title"] == "Оборачиваемость"

    def test_the_same_token_cannot_be_registered_twice_under_two_codes(self, client) -> None:
        register(client, "turnover-alerts", "111:AAA")
        with respx.mock(assert_all_called=False) as router:
            router.post(re.compile(rf"{TELEGRAM}/bot.*/getMe")).mock(return_value=Response(200, json=GET_ME))
            response = client.post(
                "/api/v1/bots",
                json={"bot_code": "reviews-alerts", "action": "upsert", "token": "111:AAA"},
                headers=auth_header(),
            )
        assert response.status_code == 409

    def test_a_rejected_token_is_not_stored(self, client) -> None:
        with respx.mock(assert_all_called=False) as router:
            router.post(re.compile(rf"{TELEGRAM}/bot.*/getMe")).mock(
                return_value=Response(200, json={"ok": False, "error_code": 401, "description": "Unauthorized"})
            )
            response = client.post(
                "/api/v1/bots",
                json={"bot_code": "dead-bot", "action": "upsert", "token": "111:dead"},
                headers=auth_header(),
            )
        assert response.status_code == 422
        send = client.post(
            "/api/v1/messages/send",
            json={"bot_code": "dead-bot", "recipient": "1", "text": "x", "idempotency_key": "k"},
            headers=auth_header(),
        )
        assert send.status_code == 404

    def test_a_validation_error_never_echoes_the_token(self, client) -> None:
        # pydantic would otherwise return the offending value inside `detail`.
        secret = "111:SUPER-SECRET-TOKEN"
        response = client.post(
            "/api/v1/bots",
            json={"bot_code": "BAD CODE", "action": "upsert", "token": secret},
            headers=auth_header(),
        )
        assert response.status_code == 422
        assert secret not in response.text

    def test_deleting_a_bot_makes_it_unknown(self, client) -> None:
        register(client)
        response = client.post(
            "/api/v1/bots",
            json={"bot_code": "turnover-alerts", "action": "delete"},
            headers=auth_header(),
        )
        assert response.status_code == 200
        send = client.post(
            "/api/v1/messages/send",
            json={"bot_code": "turnover-alerts", "recipient": "1", "text": "x", "idempotency_key": "k"},
            headers=auth_header(),
        )
        assert send.status_code == 404


class TestSending:
    # Минимальный валидный PNG 1×1 — Telegram содержимое не увидит, маршруты замоканы.
    PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="

    def _send_with_photo(self, client, *, text: str, key: str = "photo-1"):
        with respx.mock(assert_all_called=False) as router:
            message = router.post(re.compile(rf"{TELEGRAM}/bot.*/sendMessage")).mock(
                return_value=Response(200, json={"ok": True, "result": {"message_id": 501}})
            )
            photo = router.post(re.compile(rf"{TELEGRAM}/bot.*/sendPhoto")).mock(
                return_value=Response(200, json={"ok": True, "result": {"message_id": 502}})
            )
            response = client.post(
                "/api/v1/messages/send",
                json={
                    "bot_code": "turnover-alerts",
                    "recipient": "-100123",
                    "text": text,
                    "idempotency_key": key,
                    "attachment": {"kind": "photo", "png_base64": self.PNG, "caption": "Код на 22 сент.: 412"},
                },
                headers=auth_header(),
            )
            return response, message, photo

    def test_a_short_text_rides_as_the_photo_caption(self, client) -> None:
        register(client)
        response, message, photo = self._send_with_photo(client, text="Код получения на 22 сент.: 412")
        assert response.status_code == 200, response.text
        assert response.json()["message_ref"] == "502"
        assert message.call_count == 0 and photo.call_count == 1
        body = photo.calls[0].request.content
        assert b'name="caption"' in body and b"412" in body and b"image/png" in body

    def test_a_long_text_goes_first_then_the_photo(self, client) -> None:
        register(client)
        response, message, photo = self._send_with_photo(client, text="x" * 1100, key="photo-2")
        assert response.status_code == 200, response.text
        # Ссылка — на текст: по нему дедуплицирует основной сервер.
        assert response.json()["message_ref"] == "501"
        assert message.call_count == 1 and photo.call_count == 1

    def test_a_broken_attachment_is_refused_not_retried(self, client) -> None:
        register(client)
        with respx.mock(assert_all_called=False):
            response = client.post(
                "/api/v1/messages/send",
                json={
                    "bot_code": "turnover-alerts",
                    "recipient": "-100123",
                    "text": "x",
                    "idempotency_key": "photo-3",
                    "attachment": {"kind": "photo", "png_base64": "bm90IGEgcG5n", "caption": ""},
                },
                headers=auth_header(),
            )
        assert response.status_code == 422

    def _send(self, client, telegram_response: Response, key: str = "msg-1"):
        with respx.mock(assert_all_called=False) as router:
            router.post(re.compile(rf"{TELEGRAM}/bot.*/sendMessage")).mock(return_value=telegram_response)
            return client.post(
                "/api/v1/messages/send",
                json={
                    "bot_code": "turnover-alerts",
                    "recipient": "-100123",
                    "text": "Остаток кончается",
                    "idempotency_key": key,
                },
                headers=auth_header(),
            )

    def test_a_message_is_delivered(self, client) -> None:
        register(client)
        response = self._send(client, Response(200, json={"ok": True, "result": {"message_id": 777}}))
        assert response.status_code == 200
        assert response.json() == {"status": "sent", "message_ref": "777", "deduplicated": False}

    def test_a_repeat_of_the_same_key_is_not_sent_twice(self, client) -> None:
        register(client)
        self._send(client, Response(200, json={"ok": True, "result": {"message_id": 777}}))
        # No messenger route registered: a second call would fail if it happened.
        repeat = client.post(
            "/api/v1/messages/send",
            json={
                "bot_code": "turnover-alerts",
                "recipient": "-100123",
                "text": "Остаток кончается",
                "idempotency_key": "msg-1",
            },
            headers=auth_header(),
        )
        assert repeat.status_code == 200
        assert repeat.json()["deduplicated"] is True

    def test_an_unknown_chat_is_not_worth_retrying(self, client) -> None:
        register(client)
        response = self._send(
            client,
            Response(200, json={"ok": False, "error_code": 400, "description": "chat not found"}),
        )
        assert response.status_code == 422

    def test_a_messenger_outage_asks_the_caller_to_retry(self, client) -> None:
        register(client)
        outage = Response(200, json={"ok": False, "error_code": 502, "description": "Bad Gateway"})
        response = self._send(client, outage)
        assert response.status_code == 502

    def test_rate_limiting_is_passed_through_with_retry_after(self, client) -> None:
        register(client)
        response = self._send(
            client,
            Response(
                200,
                json={"ok": False, "error_code": 429, "description": "Too Many", "parameters": {"retry_after": 12}},
            ),
        )
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "12"

    def test_a_dead_token_parks_the_bot(self, client) -> None:
        register(client)
        response = self._send(
            client,
            Response(200, json={"ok": False, "error_code": 401, "description": "Unauthorized"}),
        )
        assert response.status_code == 502
        # Parked: the next call finds no active bot rather than hammering the messenger.
        again = self._send(client, Response(200, json={"ok": True, "result": {"message_id": 1}}), key="msg-2")
        assert again.status_code == 404

    def test_a_failed_send_is_not_remembered_as_delivered(self, client) -> None:
        register(client)
        self._send(client, Response(200, json={"ok": False, "error_code": 500, "description": "boom"}))
        retry = self._send(client, Response(200, json={"ok": True, "result": {"message_id": 9}}))
        assert retry.status_code == 200
        assert retry.json()["deduplicated"] is False


class TestUpdates:
    """The main server comes for updates itself: from abroad it cannot be reached."""

    def _buffer(self, client, events: list[int]) -> None:
        storage = client.app.state.storage
        for event_id in events:
            client.portal.call(
                storage.store_update,
                "turnover-alerts",
                event_id,
                f'{{"bot_code": "turnover-alerts", "event_id": {event_id}, "recipient": "555", "text": "hi"}}',
            )

    def test_fetching_needs_a_token(self, client) -> None:
        assert client.get("/api/v1/updates?bot_code=turnover-alerts").status_code == 401

    def test_everything_after_the_callers_cursor_comes_back(self, client) -> None:
        register(client)
        self._buffer(client, [10, 11, 12])

        body = client.get("/api/v1/updates?bot_code=turnover-alerts&after=10&wait=0", headers=auth_header()).json()

        # The caller's cursor is the only bookkeeping: no acknowledgement, so a
        # lost answer costs a repeat, never a lost update.
        assert [item["event_id"] for item in body["updates"]] == [11, 12]

    def test_the_batch_is_capped_and_ordered(self, client) -> None:
        register(client)
        self._buffer(client, [5, 3, 4])

        body = client.get(
            "/api/v1/updates?bot_code=turnover-alerts&after=0&limit=2&wait=0", headers=auth_header()
        ).json()

        assert [item["event_id"] for item in body["updates"]] == [3, 4]

    def test_a_quiet_channel_answers_empty_without_waiting(self, client) -> None:
        register(client)
        body = client.get("/api/v1/updates?bot_code=turnover-alerts&after=0&wait=0", headers=auth_header()).json()
        assert body == {"updates": []}
