"""Промежуточные обработчики: для каждого сообщения открываем сессию БД и находим пользователя."""
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.texts import no_access
from app.services.staff import find_or_register

# Эти команды работают даже без доступа — чтобы человек мог узнать свой ID
OPEN_COMMANDS = ("/start", "/id", "/help")


class UserMiddleware(BaseMiddleware):
    def __init__(self, sessionmaker: async_sessionmaker, open_access: bool):
        self.sm = sessionmaker
        self.open_access = open_access

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return None
        async with self.sm() as session:
            user = await find_or_register(
                session, tg_user.id, tg_user.username, tg_user.full_name, self.open_access
            )
            await session.commit()
            if user is None:
                is_open_cmd = isinstance(event, Message) and (event.text or "").startswith(OPEN_COMMANDS)
                if not is_open_cmd:
                    text = no_access(tg_user.id, tg_user.username)
                    if isinstance(event, CallbackQuery):
                        await event.answer("Нет доступа", show_alert=True)
                    elif isinstance(event, Message):
                        await event.answer(text)
                    return None
            data["session"] = session
            data["user"] = user
            return await handler(event, data)
