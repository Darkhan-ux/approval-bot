from aiogram import Router

from app.bot.handlers import admin, common, decisions, lists, new_request


def build_router() -> Router:
    router = Router()
    # Порядок важен: сначала диалоги с состояниями (заявка, отказ, исправление), потом общие команды
    router.include_routers(new_request.router, decisions.router, admin.router, lists.router, common.router)
    return router
