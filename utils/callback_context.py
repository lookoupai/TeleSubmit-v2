"""Telegram target normalization, including inaccessible and inline messages."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MessageRef:
    chat_id: Optional[object] = None
    message_id: Optional[int] = None
    inline_message_id: Optional[str] = None

    @classmethod
    def from_query(cls, query):
        if query.inline_message_id:
            return cls(inline_message_id=query.inline_message_id)
        message = query.message
        if message and message.chat and message.message_id:
            return cls(chat_id=message.chat.id, message_id=message.message_id)
        raise ValueError("Callback message is inaccessible")

    def parameters(self):
        if self.inline_message_id:
            return {"inline_message_id": self.inline_message_id}
        if self.chat_id is None or self.message_id is None:
            raise ValueError("Missing message target")
        return {"chat_id": self.chat_id, "message_id": self.message_id}

    async def edit_markup(self, bot, **kwargs):
        return await bot.edit_message_reply_markup(**self.parameters(), **kwargs)


async def edit_callback_content(query, text, *, reply_markup=None, **kwargs):
    """Photo payment receipts require caption edits; inline messages lack a body."""
    message = query.message
    if message and (
        getattr(message, "photo", None)
        or getattr(message, "document", None)
        or getattr(message, "video", None)
    ):
        kwargs.pop("disable_web_page_preview", None)
        kwargs.pop("link_preview_options", None)
        return await query.edit_message_caption(
            caption=text, reply_markup=reply_markup, **kwargs
        )
    return await query.edit_message_text(text, reply_markup=reply_markup, **kwargs)
