"""Кнопки бота."""
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.models import Request, Status
from app.workflow_config import Stage, WorkflowConfig

BTN_NEW = "📝 Новая заявка"
BTN_MY = "📋 Мои заявки"
BTN_INBOX = "📥 Ждут моего решения"


class TypeCb(CallbackData, prefix="t"):
    code: str


class DecisionCb(CallbackData, prefix="d"):
    action: str  # ok | no
    req: int
    stage: int


class ViewCb(CallbackData, prefix="v"):
    req: int


class CancelReqCb(CallbackData, prefix="c"):
    req: int


class AdminCb(CallbackData, prefix="a"):
    action: str  # annul | pick (выбрать этап) | ret (вернуть на этап)
    req: int
    stage: str = ""


# Кнопки мастера создания заявки
NR_SKIP = "nr:skip"
NR_SEND = "nr:send"
NR_ABORT = "nr:abort"
REJ_ABORT = "rej:abort"
ADMIN_ABORT = "adm:abort"


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_NEW)], [KeyboardButton(text=BTN_MY), KeyboardButton(text=BTN_INBOX)]],
        resize_keyboard=True,
    )


def types_kb(wf: WorkflowConfig) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for t in wf.request_types:
        kb.button(text=t.title, callback_data=TypeCb(code=t.code))
    kb.button(text="✖️ Отмена", callback_data=NR_ABORT)
    kb.adjust(1)
    return kb.as_markup()


def abort_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data=NR_ABORT)
    return kb.as_markup()


def photo_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Без вложения ➡️", callback_data=NR_SKIP)
    kb.button(text="✖️ Отмена", callback_data=NR_ABORT)
    kb.adjust(1)
    return kb.as_markup()


def confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Отправить", callback_data=NR_SEND)
    kb.button(text="✖️ Отмена", callback_data=NR_ABORT)
    return kb.as_markup()


def decision_kb(req: Request, wf: WorkflowConfig) -> InlineKeyboardMarkup | None:
    if not req.is_open or req.stage_index is None:
        return None
    stage = wf.stages[req.stage_index]
    kb = InlineKeyboardBuilder()
    kb.button(text=stage.ok_label, callback_data=DecisionCb(action="ok", req=req.id, stage=req.stage_index))
    kb.button(text="❌ Отклонить", callback_data=DecisionCb(action="no", req=req.id, stage=req.stage_index))
    kb.button(text="🔎 История", callback_data=ViewCb(req=req.id))
    kb.adjust(2, 1)
    return kb.as_markup()


def reject_abort_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Не отклонять", callback_data=REJ_ABORT)
    return kb.as_markup()


def my_list_kb(requests: list[Request]) -> InlineKeyboardMarkup | None:
    if not requests:
        return None
    kb = InlineKeyboardBuilder()
    for r in requests:
        kb.button(text=f"№{r.id}", callback_data=ViewCb(req=r.id))
    kb.adjust(5)
    return kb.as_markup()


def detail_kb(req: Request, is_author: bool, can_admin: bool) -> InlineKeyboardMarkup | None:
    kb = InlineKeyboardBuilder()
    if is_author and req.is_open:
        kb.button(text="🚫 Отозвать заявку", callback_data=CancelReqCb(req=req.id))
    if can_admin and req.status != Status.ANNULLED:
        kb.button(text="↩️ Вернуть на этап", callback_data=AdminCb(action="pick", req=req.id))
        kb.button(text="🗑 Аннулировать", callback_data=AdminCb(action="annul", req=req.id))
    kb.adjust(1)
    return kb.as_markup() if list(kb.buttons) else None


def stages_kb(req: Request, stages: list[Stage]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for stage in stages:
        kb.button(text=stage.title, callback_data=AdminCb(action="ret", req=req.id, stage=stage.code))
    kb.button(text="✖️ Отмена", callback_data=ADMIN_ABORT)
    kb.adjust(1)
    return kb.as_markup()


def admin_abort_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data=ADMIN_ABORT)
    return kb.as_markup()
