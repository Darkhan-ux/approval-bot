"""«Мои заявки», «Ждут моего решения», карточка с историей, /r по номеру, отзыв заявки."""
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.bot.keyboards import BTN_INBOX, BTN_MY, CancelReqCb, ViewCb, detail_kb, my_list_kb
from app.bot.notifier import Notifier
from app.bot.texts import request_card, short_line, timeline
from app.models import Request, User
from app.services.approval import ApprovalService, WorkflowError
from app.workflow_config import WorkflowConfig

router = Router()


@router.message(F.text == BTN_MY)
@router.message(Command("my"))
async def my_requests(message: Message, session: AsyncSession, user: User,
                      approval: ApprovalService, wf: WorkflowConfig):
    items = await approval.by_author(session, user, limit=10)
    if not items:
        await message.answer("У вас пока нет заявок. Нажмите «📝 Новая заявка».")
        return
    lines = ["<b>Ваши последние заявки:</b>", ""] + [short_line(r, wf) for r in items]
    lines.append("\nНажмите на номер, чтобы открыть историю:")
    await message.answer("\n".join(lines), reply_markup=my_list_kb(items))


@router.message(F.text == BTN_INBOX)
@router.message(Command("inbox"))
async def inbox(message: Message, session: AsyncSession, user: User,
                approval: ApprovalService, notifier: Notifier):
    items = await approval.inbox(session, user, limit=10)
    if not items:
        await message.answer("🎉 Нет заявок, которые ждут вашего решения.")
        return
    await message.answer(f"Ждут вашего решения: <b>{len(items)}</b>")
    for req in items:
        await notifier.send_card(user, req, header="📥 ")


async def show_request(message: Message, req: Request, user: User,
                       approval: ApprovalService, wf: WorkflowConfig, tz: ZoneInfo) -> None:
    """Карточка заявки с историей и кнопками, доступными этому человеку."""
    text = request_card(req, wf, tz) + "\n\n" + timeline(req, wf, tz)
    kb = detail_kb(req, is_author=req.author_id == user.id, can_admin=approval.can_admin(req, user))
    await message.answer(text[:4096], reply_markup=kb)


@router.callback_query(ViewCb.filter())
async def view(cb: CallbackQuery, callback_data: ViewCb, session: AsyncSession, user: User,
               approval: ApprovalService, wf: WorkflowConfig, tz: ZoneInfo):
    try:
        req = await approval.get(session, callback_data.req)
    except WorkflowError as e:
        await cb.answer(e.message, show_alert=True)
        return
    if not approval.can_view(req, user):
        await cb.answer("Нет доступа к этой заявке", show_alert=True)
        return
    await show_request(cb.message, req, user, approval, wf, tz)
    await cb.answer()


@router.message(Command("r"))
async def open_by_number(message: Message, command: CommandObject, session: AsyncSession, user: User,
                         approval: ApprovalService, wf: WorkflowConfig, tz: ZoneInfo):
    """/r 161 — открыть заявку по номеру."""
    arg = (command.args or "").strip().lstrip("№#")
    if not arg.isdigit():
        await message.answer("Укажите номер заявки, например: <code>/r 161</code>")
        return
    try:
        req = await approval.get(session, int(arg))
    except WorkflowError as e:
        await message.answer(e.message)
        return
    if not approval.can_view(req, user):
        await message.answer("Нет доступа к этой заявке")
        return
    await show_request(message, req, user, approval, wf, tz)


@router.callback_query(CancelReqCb.filter())
async def cancel_request(cb: CallbackQuery, callback_data: CancelReqCb, session: AsyncSession, user: User,
                         approval: ApprovalService, notifier: Notifier):
    try:
        outcome = await approval.cancel(session, callback_data.req, user)
        await session.commit()
    except (WorkflowError, StaleDataError) as e:
        await session.rollback()
        await cb.answer(e.message if isinstance(e, WorkflowError) else "Заявка только что изменилась", show_alert=True)
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(f"🚫 Заявка №{callback_data.req} отозвана.")
    await cb.answer()
    await notifier.after(outcome)
