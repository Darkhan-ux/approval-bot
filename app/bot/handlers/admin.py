"""Исправление ошибок администратором: «Аннулировать» и «Вернуть на этап» (оба — с причиной)."""
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.bot.keyboards import ADMIN_ABORT, AdminCb, admin_abort_kb, main_menu, stages_kb
from app.bot.notifier import Notifier
from app.models import Status, User
from app.services.approval import ApprovalService, InvalidInput, WorkflowError
from app.workflow_config import WorkflowConfig

router = Router()


class AdminFlow(StatesGroup):
    reason = State()


async def _load(cb: CallbackQuery, session: AsyncSession, user: User, approval: ApprovalService, req_id: int):
    """Заявка, если этот человек может её исправлять; иначе показывает ошибку и возвращает None."""
    try:
        req = await approval.get(session, req_id)
    except WorkflowError as e:
        await cb.answer(e.message, show_alert=True)
        return None
    if not approval.can_admin(req, user):
        await cb.answer("Это может сделать только администратор", show_alert=True)
        return None
    if req.status == Status.ANNULLED:
        await cb.answer(f"Заявка №{req.id} уже аннулирована", show_alert=True)
        return None
    return req


@router.callback_query(AdminCb.filter(F.action == "pick"))
async def pick_stage(cb: CallbackQuery, callback_data: AdminCb, session: AsyncSession, user: User,
                     approval: ApprovalService):
    req = await _load(cb, session, user, approval, callback_data.req)
    if req is None:
        return
    stages = approval.returnable_stages(req)
    if not stages:
        await cb.answer("Вернуть эту заявку некуда", show_alert=True)
        return
    await cb.message.answer(f"↩️ На какой этап вернуть заявку №{req.id}?", reply_markup=stages_kb(req, stages))
    await cb.answer()


@router.callback_query(AdminCb.filter(F.action.in_({"ret", "annul"})))
async def ask_reason(cb: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                     user: User, approval: ApprovalService, wf: WorkflowConfig):
    req = await _load(cb, session, user, approval, callback_data.req)
    if req is None:
        return
    await state.set_state(AdminFlow.reason)
    await state.update_data(action=callback_data.action, req=req.id, stage=callback_data.stage)
    if callback_data.action == "annul":
        prompt = f"🗑 Почему аннулировать заявку №{req.id}? Причину увидят автор и все в истории:"
    else:
        stage = escape(wf.stage_title(callback_data.stage))
        prompt = f"↩️ Почему вернуть заявку №{req.id} на этап «{stage}»? Причина обязательна:"
    await cb.message.answer(prompt, reply_markup=admin_abort_kb())
    await cb.answer()


@router.callback_query(F.data == ADMIN_ABORT)
async def abort(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Отменено, заявка не изменилась.")
    await cb.answer()


@router.message(AdminFlow.reason, F.text)
async def apply(message: Message, state: FSMContext, session: AsyncSession, user: User,
                approval: ApprovalService, notifier: Notifier, wf: WorkflowConfig):
    data = await state.get_data()
    try:
        if data["action"] == "annul":
            outcome = await approval.annul(session, data["req"], user, message.text)
        else:
            outcome = await approval.return_to_stage(session, data["req"], data["stage"], user, message.text)
        await session.commit()
    except InvalidInput as e:
        await message.answer(e.message, reply_markup=admin_abort_kb())
        return
    except (WorkflowError, StaleDataError) as e:
        await session.rollback()
        await state.clear()
        await message.answer(e.message if isinstance(e, WorkflowError) else "Заявка только что изменилась, "
                             "откройте её заново")
        return
    await state.clear()
    if data["action"] == "annul":
        done = f"🗑 Заявка №{data['req']} аннулирована. Автор получит причину."
    else:
        done = (f"↩️ Заявка №{data['req']} возвращена на этап «{escape(wf.stage_title(data['stage']))}». "
                "Ответственный получит её заново.")
    await message.answer(done, reply_markup=main_menu())
    await notifier.after(outcome)


@router.message(AdminFlow.reason)
async def need_text(message: Message):
    await message.answer("Напишите причину текстом.", reply_markup=admin_abort_kb())
