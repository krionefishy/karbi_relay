"""Wire contract v1.

`recipient` is an opaque string on purpose: the main server receives it in an
update, stores it and hands it back when it wants a message delivered, without
ever interpreting it. Swapping the messenger changes what is inside the string,
not the contract.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

BotCode = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")]


class BotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bot_code: BotCode
    action: Literal["upsert", "delete"] = "upsert"
    # SecretStr keeps the token out of tracebacks and repr. The validation error
    # handler in app.py keeps it out of 422 bodies, which pydantic would
    # otherwise echo back verbatim.
    token: SecretStr | None = None


class BotResponse(BaseModel):
    bot_code: str
    title: str
    invite_link_template: str


class Attachment(BaseModel):
    """A picture next to the text — a QR code, for now.

    Bytes come base64-encoded inside JSON: one request, one idempotency key,
    no multipart on the channel. Two megabytes is far above any QR.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["photo"] = "photo"
    png_base64: Annotated[str, Field(min_length=1, max_length=2_800_000)]
    caption: Annotated[str, Field(max_length=1024)] = ""


class SendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bot_code: BotCode
    recipient: Annotated[str, Field(min_length=1, max_length=128)]
    text: Annotated[str, Field(min_length=1, max_length=4096)]
    idempotency_key: Annotated[str, Field(min_length=1, max_length=128)]
    attachment: Attachment | None = None


class SendResponse(BaseModel):
    status: Literal["sent"] = "sent"
    message_ref: str | None = None
    deduplicated: bool = False


class UpdatesResponse(BaseModel):
    """Everything buffered after the caller's cursor, oldest first.

    No acknowledgement endpoint on purpose: the main server's own cursor is the
    only bookkeeping there is, and it tells us where to continue on every call.
    """

    updates: list[dict[str, object]]


class BotReadiness(BaseModel):
    last_successful_poll_at: str | None = None
    last_buffered_event_id: int | None = None
    consecutive_errors: int = 0
    last_error: str = ""
    parked: bool = False


class ReadyResponse(BaseModel):
    ok: bool
    bots: dict[str, BotReadiness]
    pending_dedupe_keys: int
    buffered_updates: int = 0
