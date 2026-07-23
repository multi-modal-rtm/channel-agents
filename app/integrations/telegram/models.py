"""Pydantic models for Telegram Bot API webhook payloads."""

from __future__ import annotations

from pydantic import BaseModel


class TelegramUser(BaseModel):
    id: int
    is_bot: bool = False
    first_name: str = ""
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None


class TelegramChat(BaseModel):
    id: int
    type: str  # private | group | supergroup | channel
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    title: str | None = None


class TelegramMessage(BaseModel):
    message_id: int
    from_: TelegramUser | None = None
    chat: TelegramChat
    date: int
    text: str | None = None
    caption: str | None = None

    model_config = {"populate_by_name": True}

    @classmethod
    def model_validate(cls, obj, **kwargs):
        if isinstance(obj, dict) and "from" in obj and "from_" not in obj:
            obj = {**obj, "from_": obj.pop("from")}
        return super().model_validate(obj, **kwargs)

    @property
    def effective_text(self) -> str | None:
        return self.text or self.caption


class TelegramUpdate(BaseModel):
    update_id: int
    message: TelegramMessage | None = None
    edited_message: TelegramMessage | None = None

    @property
    def effective_message(self) -> TelegramMessage | None:
        return self.message or self.edited_message
