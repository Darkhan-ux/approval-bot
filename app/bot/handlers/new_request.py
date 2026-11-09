"""Мастер подачи заявки: тип → сумма → описание → вложение → подтверждение."""
from decimal import Decimal
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import (
    BTN_NEW,
    NR_ABORT,
    NR_SEND,
    NR_SKIP,
    TypeCb,
    abort_kb,
    confirm_kb,
    main_menu,
    photo_kb,
    types_kb,
)
from app.bot.notifier import Notifier
from app.bot.texts import status_line
from app.formatting import money
from app.models import User
from app.services.approval import ApprovalService, WorkflowError, parse_amount
from app.workflow_config import WorkflowConfig

router = Router()


class NewRequest(StatesGroup):
    type = State()
    amount = State()
    description = State()
    attachment = State()
    confirm = State()


@router.message(F.text == BTN_NEW)
@router.message(Command("new"))
async def start_new(message: Message, state: FSMContext, wf: WorkflowConfig):
    await state.clear()
    await state.set_state(NewRequest.type)
    await message.answer("<b>Новая заявка</b>\n\nШаг 1 из 4. Выберите тип:", reply_markup=types_kb(wf))


@router.callback_query(F.data == NR_ABORT)
async def abort(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Заявка не отправлена.")
    await cb.message.answer("Чем ещё помочь?", reply_markup=main_menu())
    await cb.answer()


@router.callback_query(NewRequest.type, TypeCb.filter())
async def chose_type(cb: CallbackQuery, callback_data: TypeCb, state: FSMContext, wf: WorkflowConfig):
    await state.update_data(type_code=callback_data.code)
    await state.set_state(NewRequest.amount)
    await cb.message.edit_text(
        f"Тип: <b>{escape(wf.type_title(callback_data.code))}</b>\n\n"
        f"Шаг 2 из 4. Введите сумму ({wf.currency}), например: <code>75000</code>",
        reply_markup=abort_kb(),
    )
    await cb.answer()


@router.message(NewRequest.amount, F.text)
async def got_amount(message: Message, state: FSMContext, wf: WorkflowConfig):
    try:
        amount = parse_amount(message.text)
    except WorkflowError as e:
        await message.answer(e.message, reply_markup=abort_kb())
        return
    await state.update_data(amount=str(amount))
    await state.set_state(NewRequest.description)
    await message.answer(
        f"Сумма: <b>{money(amount, wf.currency)}</b>\n\n"
        "Шаг 3 из 4. Опишите, что и зачем нужно (поставщик, назначение, срок):",
        reply_markup=abort_kb(),
    )


@router.message(NewRequest.description, F.text)
async def got_description(message: Message, state: FSMContext):
    text = message.text.strip()
    if len(text) < 3:
        await message.answer("Слишком коротко — опишите хотя бы парой слов.")
        return
    if len(text) > 2000:
        await message.answer("Слишком длинно — уложитесь в 2000 символов.")
        return
    await state.update_data(description=text)
    await state.set_state(NewRequest.attachment)
    await message.answer(
        "Шаг 4 из 4. Пришлите фото или файл (счёт, товар, ценник) — или нажмите «Без вложения».",
        reply_markup=photo_kb(),
    )


@router.message(NewRequest.attachment, F.photo)
async def got_photo(message: Message, state: FSMContext, wf: WorkflowConfig):
    await state.update_data(attachment_file_id=message.photo[-1].file_id, attachment_kind="photo")
    await show_confirm(message, state, wf)


@router.message(NewRequest.attachment, F.document)
async def got_document(message: Message, state: FSMContext, wf: WorkflowConfig):
    await state.update_data(attachment_file_id=message.document.file_id, attachment_kind="document")
    await show_confirm(message, state, wf)


@router.callback_query(NewRequest.attachment, F.data == NR_SKIP)
async def skip_attachment(cb: CallbackQuery, state: FSMContext, wf: WorkflowConfig):
    await cb.message.edit_reply_markup(reply_markup=None)
    await show_confirm(cb.message, state, wf)
    await cb.answer()


@router.message(NewRequest.attachment)
async def wrong_attachment(message: Message):
    await message.answer("Пришлите фото или файл — или нажмите «Без вложения».", reply_markup=photo_kb())


@router.message(NewRequest.amount)
@router.message(NewRequest.description)
async def need_text(message: Message):
    await message.answer("Пожалуйста, ответьте текстом.")


async def show_confirm(message: Message, state: FSMContext, wf: WorkflowConfig):
    data = await state.get_data()
    await state.set_state(NewRequest.confirm)
    attach = {"photo": "📷 фото", "document": "📎 файл"}.get(data.get("attachment_kind"), "нет")
    await message.answer(
        "<b>Проверьте заявку:</b>\n\n"
        f"Тип: {escape(wf.type_title(data['type_code']))}\n"
        f"Сумма: <b>{money(Decimal(data['amount']), wf.currency)}</b>\n"
        f"Описание: {escape(data['description'])}\n"
        f"Вложение: {attach}",
        reply_markup=confirm_kb(),
    )


@router.callback_query(NewRequest.confirm, F.data == NR_SEND)
async def send(cb: CallbackQuery, state: FSMContext, session: AsyncSession, user: User,
               approval: ApprovalService, notifier: Notifier, wf: WorkflowConfig):
    data = await state.get_data()
    try:
        outcome = await approval.create(
            session, user,
            type_code=data["type_code"],
            amount=Decimal(data["amount"]),
            description=data["description"],
            attachment_file_id=data.get("attachment_file_id"),
            attachment_kind=data.get("attachment_kind"),
        )
        await session.commit()
    except WorkflowError as e:
        await session.rollback()
        await cb.answer(e.message, show_alert=True)
        return
    await state.clear()
    req = outcome.request
    await cb.message.edit_text(
        f"✅ <b>Заявка №{req.id} отправлена</b>\n\n{status_line(req, wf)}\n\n"
        "Я сообщу, когда по ней примут решение."
    )
    await cb.message.answer("Что дальше?", reply_markup=main_menu())
    await cb.answer()
    await notifier.after(outcome)


@router.callback_query(F.data.in_({NR_SEND, NR_SKIP}))
async def stale_wizard_button(cb: CallbackQuery):
    await cb.answer("Эта форма уже неактуальна — начните новую заявку", show_alert=True)
