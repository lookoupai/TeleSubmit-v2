"""Small public-API transport for fields/methods not covered by pinned PTB.

No transport retries: a timeout of a sending method has an unknown outcome.
"""

import asyncio
import aiohttp


class TelegramAPIError(RuntimeError):
    def __init__(self, category, *, retry_after=None):
        self.category, self.retry_after = category, retry_after
        super().__init__("Telegram API: " + category)


class TelegramAPI:
    METHODS = frozenset(
        {
            "sendMessage",
            "editMessageReplyMarkup",
            "setMySettings",
            "getMe",
            "sendRichMessage",
            "editMessageText",
            "sendWidget",
            "answerWidgetQuery",
            "editMessageWidgetData",
        }
    )

    def __init__(self, token, *, session=None):
        self._token = token
        self._session = session
        self._owns_session = session is None
        self._limit = asyncio.Semaphore(4)
        self._answers = asyncio.Semaphore(4)

    async def call(self, method, data):
        if method not in self.METHODS:
            raise ValueError("Bot API method not allowed by this adapter")
        if self._session is None:
            self._session = aiohttp.ClientSession()
        limit = self._answers if method == "answerWidgetQuery" else self._limit
        async with limit:
            try:
                async with self._session.post(
                    f"https://api.telegram.org/bot{self._token}/{method}",
                    json=data,
                    timeout=aiohttp.ClientTimeout(total=10),
                    allow_redirects=False,
                ) as response:
                    if response.status >= 500:
                        raise TelegramAPIError("unknown_delivery")
                    result = await response.json()
                    if not isinstance(result, dict):
                        raise TelegramAPIError("unknown_delivery")
                    if result.get("ok") is True:
                        return result.get("result")
                    code = result.get("error_code", response.status)
                    description = str(result.get("description", "")).lower()
                    category = (
                        "rate_limited"
                        if code == 429
                        else (
                            "forbidden"
                            if code in (401, 403)
                            else (
                                "unsupported"
                                if code == 404
                                else (
                                    "invalid_payload"
                                    if code == 400
                                    else "unknown_delivery"
                                )
                            )
                        )
                    )
                    if "method not found" in description:
                        category = "unsupported"
                    if code == 400 and (
                        "message to edit not found" in description
                        or "message_id_invalid" in description
                    ):
                        category = "missing_message"
                    if code == 400 and "message is not modified" in description:
                        category = "unchanged"
                    raise TelegramAPIError(
                        category,
                        retry_after=(result.get("parameters") or {}).get("retry_after"),
                    )
            except TelegramAPIError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                # Do not propagate a raw exception that can contain a token-bearing URL.
                raise TelegramAPIError("unknown_delivery") from None

    async def close(self):
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
