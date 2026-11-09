from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.keyboards import main_menu
from app.bot.texts import HELP, no_access, welcome
from app.models import User

router = Router()


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, user: User | None, open_access: bool):
    await state.clear()
    if user is None:
        await message.answer(no_access(message.from_user.id, message.from_user.username))
        return
    await message.answer(welcome(user, open_access), reply_markup=main_menu())


@router.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(HELP)


@router.message(Command("id"))
async def my_id(message: Message):
    u = message.from_user
    nick = f"@{u.username}" if u.username else "ника нет"
    await message.answer(f"Ваш Telegram ID: <code>{u.id}</code>\nНик: {nick}")


@router.message()
async def fallback(message: Message, user: User | None):
    if user is None:
        return
    await message.answer("Не понял. Выберите действие в меню ниже или наберите /help", reply_markup=main_menu())
