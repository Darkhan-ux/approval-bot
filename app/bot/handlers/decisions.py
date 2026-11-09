"""Кнопки «Одобрить» / «Отклонить» и ввод причины отказа."""
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.bot.keyboards import REJ_ABORT, DecisionCb, main_menu, reject_abort_kb
from app.bot.notifier import Notifier
from app.models import User
from app.services.approval import AlreadyProcessed, ApprovalService, InvalidInput, WorkflowError

router = Router()


class RejectFlow(StatesGroup):
    reason = State()


async def mark_message(bot: Bot, chat_id: int, message_id: int, note: str, original: Message | None = None):
    """Убирает кнопки у карточки заявки и дописывает итог."""
    try:
        body = (original.html_text or "") + f"\n\n<b>{note}</b>" if original is not None else ""
        if original is not None and original.text:
            await original.edit_text(body, reply_markup=None)
        elif original is not None and original.caption and len(body) <= 1024:
            await original.edit_caption(caption=body, reply_markup=None)
        else:
            # Подпись к фото не влезает (или карточки нет под рукой) — просто убираем кнопки
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except TelegramAPIError:
        pass  # сообщение могли удалить — не страшно


@router.callback_query(DecisionCb.filter(F.action == "ok"))
async def approve(cb: CallbackQuery, callback_data: DecisionCb, session: AsyncSession, user: User,
                  approval: ApprovalService, notifier: Notifier, bot: Bot):
    try:
        outcome = await approval.approve(session, callback_data.req, callback_data.stage, user)
        await session.commit()
    except (WorkflowError, StaleDataError) as e:
        await session.rollback()
        msg = e.message if isinstance(e, WorkflowError) else "Заявку только что обработал кто-то другой"
        await cb.answer(msg, show_alert=True)
        if isinstance(e, (AlreadyProcessed, StaleDataError)):
            await mark_message(bot, cb.message.chat.id, cb.message.message_id, "Уже обработана", cb.message)
        return
    label = "✅ Вы одобрили" if outcome.action == "approved" else "✅ Вы отметили исполнение"
    await mark_message(bot, cb.message.chat.id, cb.message.message_id, label, cb.message)
    await cb.answer("Готово")
    await notifier.after(outcome)


@router.callback_query(DecisionCb.filter(F.action == "no"))
async def reject_start(cb: CallbackQuery, callback_data: DecisionCb, state: FSMContext,
                       session: AsyncSession, user: User, approval: ApprovalService, bot: Bot):
    try:
        await approval.check_can_act(session, callback_data.req, callback_data.stage, user)
    except WorkflowError as e:
        await cb.answer(e.message, show_alert=True)
        if isinstance(e, AlreadyProcessed):
            await mark_message(bot, cb.message.chat.id, cb.message.message_id, "Уже обработана", cb.message)
        return
    await state.set_state(RejectFlow.reason)
    await state.update_data(req=callback_data.req, stage=callback_data.stage,
                            chat_id=cb.message.chat.id, message_id=cb.message.message_id)
    await cb.message.answer(
        f"✍️ Напишите причину отказа по заявке №{callback_data.req} — она обязательна, автор её увидит:",
        reply_markup=reject_abort_kb(),
    )
    await cb.answer()


@router.callback_query(F.data == REJ_ABORT)
async def reject_abort(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Хорошо, заявка не отклонена — кнопки в карточке по-прежнему работают.")
    await cb.answer()


@router.message(RejectFlow.reason, F.text)
async def reject_reason(message: Message, state: FSMContext, session: AsyncSession, user: User,
                        approval: ApprovalService, notifier: Notifier, bot: Bot):
    data = await state.get_data()
    try:
        outcome = await approval.reject(session, data["req"], data["stage"], user, message.text)
        await session.commit()
    except InvalidInput as e:
        await message.answer(e.message, reply_markup=reject_abort_kb())
        return
    except (WorkflowError, StaleDataError) as e:
        await session.rollback()
        await state.clear()
        await message.answer(e.message if isinstance(e, WorkflowError) else "Заявку уже обработал кто-то другой")
        return
    await state.clear()
    await mark_message(bot, data["chat_id"], data["message_id"], "❌ Отклонена")
    await message.answer(f"❌ Заявка №{data['req']} отклонена. Автор получит причину.", reply_markup=main_menu())
    await notifier.after(outcome)


@router.message(RejectFlow.reason)
async def reject_need_text(message: Message):
    await message.answer("Напишите причину текстом.", reply_markup=reject_abort_kb())
