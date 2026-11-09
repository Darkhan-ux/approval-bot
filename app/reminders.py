"""Фоновая проверка зависших заявок."""
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.notifier import Notifier
from app.services.approval import ApprovalService

log = logging.getLogger(__name__)


async def send_reminders(sessionmaker: async_sessionmaker, approval: ApprovalService, notifier: Notifier) -> int:
    try:
        async with sessionmaker() as session:
            overdue = await approval.overdue(session)
            to_notify = []
            for req in overdue:
                to_notify.append((req, req.current_assignees()))
                approval.mark_reminded(req)
            await session.commit()
        for req, assignees in to_notify:
            for user in assignees:
                await notifier.remind(user, req)
        if to_notify:
            log.info("Отправлены напоминания по %s заявкам", len(to_notify))
            if notifier.sync:
                notifier.sync.kick()
        return len(to_notify)
    except Exception:
        log.exception("Ошибка при отправке напоминаний")
        return 0
