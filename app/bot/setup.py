"""Сборка бота: подключаем обработчики и передаём им общие объекты."""
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.handlers import build_router
from app.bot.middlewares import UserMiddleware
from app.bot.notifier import Notifier
from app.services.approval import ApprovalService
from app.services.sync import SheetSync
from app.workflow_config import WorkflowConfig

COMMANDS = [
    BotCommand(command="new", description="Новая заявка"),
    BotCommand(command="my", description="Мои заявки"),
    BotCommand(command="inbox", description="Ждут моего решения"),
    BotCommand(command="r", description="Открыть заявку по номеру: /r 123"),
    BotCommand(command="help", description="Как это работает"),
    BotCommand(command="id", description="Мой Telegram ID"),
]


# Профиль бота: «О боте» и текст на экране до нажатия «Запустить»
SHORT_DESCRIPTION = (
    "Согласование заявок на закупку и оплату: руководитель → директор → бухгалтер. Журнал в Google Таблице."
)
DESCRIPTION = (
    "Бот для согласования заявок внутри компании.\n\n"
    "• Подайте заявку: тип, сумма, описание, фото или файл\n"
    "• Она пройдёт маршрут: руководитель → директор (от 500 000 ₸) → бухгалтер\n"
    "• Отказ — только с причиной, зависшие заявки — с напоминаниями\n"
    "• Ошибки исправляет администратор, история не удаляется\n"
    "• Всё пишется в Google Таблицу, аналитика — на дашборде\n\n"
    "Нажмите «Запустить» и подайте заявку."
)


def create_bot(token: str) -> Bot:
    return Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def create_dispatcher(
    bot: Bot, sessionmaker: async_sessionmaker, approval: ApprovalService, wf: WorkflowConfig,
    tz: ZoneInfo, sync: SheetSync | None, open_access: bool,
) -> tuple[Dispatcher, Notifier]:
    notifier = Notifier(bot, wf, tz, sync, open_access)
    # Всё, что передано сюда, бот подставляет в обработчики по имени аргумента
    dp = Dispatcher(
        storage=MemoryStorage(),
        approval=approval, wf=wf, tz=tz, notifier=notifier, open_access=open_access,
    )
    users = UserMiddleware(sessionmaker, open_access)
    dp.message.middleware(users)
    dp.callback_query.middleware(users)
    dp.include_router(build_router())

    async def on_startup():
        await bot.set_my_commands(COMMANDS)
        await bot.set_my_short_description(SHORT_DESCRIPTION)
        await bot.set_my_description(DESCRIPTION)

    dp.startup.register(on_startup)
    return dp, notifier
