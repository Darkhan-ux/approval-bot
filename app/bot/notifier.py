"""Рассылка уведомлений по итогам действий с заявкой."""
import logging
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.bot.keyboards import decision_kb
from app.bot.texts import last_comment, outcome_for_author, request_card
from app.formatting import fmt_hours
from app.models import Action, Request, User, utcnow
from app.services.approval import Outcome
from app.services.sync import SheetSync
from app.workflow_config import WorkflowConfig

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot: Bot, wf: WorkflowConfig, tz: ZoneInfo, sync: SheetSync | None, open_access: bool):
        self.bot = bot
        self.wf = wf
        self.tz = tz
        self.sync = sync
        self.open_access = open_access

    async def after(self, outcome: Outcome) -> None:
        """Вызывается после каждого действия: уведомляет людей и обновляет таблицу."""
        if self.sync:
            self.sync.kick()
        req = outcome.request
        stage_title = outcome.stage.title if outcome.stage else None

        if outcome.action == Action.CANCELLED:
            for user in outcome.previous_assignees:
                if user.id != req.author_id:
                    await self._send(user, f"🚫 Заявка №{req.id} отозвана автором — решение больше не нужно.")
            return

        # Исправление администратором: у прежних ответственных решение больше не ждут
        if outcome.action in (Action.ANNULLED, Action.RETURNED):
            new_ids = {u.id for u in outcome.new_assignees}
            note = "аннулирована" if outcome.action == Action.ANNULLED else "возвращена на другой этап"
            for user in outcome.previous_assignees:
                if user.id not in new_ids and user.id != req.author_id:
                    await self._send(user, f"ℹ️ Заявка №{req.id} {note} администратором — "
                                           "ваше решение по ней больше не нужно.")

        # Автору — что случилось с его заявкой (о создании он уже знает)
        if outcome.action != Action.CREATED:
            await self._send(req.author, outcome_for_author(outcome.action, req, self.wf, stage_title))
        elif outcome.finished:
            await self._send(req.author, outcome_for_author(outcome.action, req, self.wf, None))

        # Следующим ответственным — карточку с кнопками
        header = ""
        if outcome.action == Action.RETURNED:
            reason = escape(last_comment(req, Action.RETURNED))
            header = f"↩️ <b>Заявка возвращена вам на повторное решение</b>\nПричина: <i>{reason}</i>\n\n"
        for user in outcome.new_assignees:
            await self.send_card(user, req, header=header)

    async def send_card(self, user: User, req: Request, header: str = "") -> None:
        if not user.tg_id:
            return
        if self.open_access and req.is_open:
            stage = escape(self.wf.stage_title(req.stage_code))
            header = header or f"📨 <b>Нужно ваше решение</b> · этап «{stage}»\n\n"
        elif not header:
            header = "📨 <b>Нужно ваше решение</b>\n\n"
        text = header + request_card(req, self.wf, self.tz, short_description=bool(req.attachment_file_id))
        kb = decision_kb(req, self.wf)
        try:
            if req.attachment_kind == "photo":
                await self.bot.send_photo(user.tg_id, req.attachment_file_id, caption=text, reply_markup=kb)
            elif req.attachment_kind == "document":
                await self.bot.send_document(user.tg_id, req.attachment_file_id, caption=text, reply_markup=kb)
            else:
                await self.bot.send_message(user.tg_id, text, reply_markup=kb)
        except TelegramAPIError as e:
            log.warning("Не удалось отправить заявку №%s пользователю %s: %s", req.id, user.id, e)

    async def remind(self, user: User, req: Request) -> None:
        waited = (utcnow() - req.stage_entered_at).total_seconds() / 3600
        header = f"⏰ <b>Напоминание:</b> заявка ждёт вашего решения уже {fmt_hours(waited)}\n\n"
        await self.send_card(user, req, header=header)

    async def _send(self, user: User, text: str) -> None:
        if not user.tg_id:
            return
        try:
            await self.bot.send_message(user.tg_id, text)
        except TelegramAPIError as e:
            log.warning("Не удалось отправить сообщение пользователю %s: %s", user.id, e)
