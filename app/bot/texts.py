"""Тексты сообщений бота."""
from html import escape
from zoneinfo import ZoneInfo

from app.formatting import fmt_dt, fmt_hours, money
from app.models import ACTION_TITLES, Action, Request, Status, User, utcnow
from app.workflow_config import WorkflowConfig

STATUS_ICONS = {
    Status.IN_PROGRESS: "⏳",
    Status.DONE: "✅",
    Status.REJECTED: "❌",
    Status.CANCELLED: "🚫",
    Status.ANNULLED: "🗑",
}


def status_line(req: Request, wf: WorkflowConfig) -> str:
    if req.status == Status.IN_PROGRESS:
        names = ", ".join(escape(u.full_name) for u in req.current_assignees())
        line = f"⏳ На этапе «{escape(wf.stage_title(req.stage_code))}»"
        return f"{line} — {names}" if names else line
    if req.status == Status.DONE:
        return "✅ Исполнена"
    if req.status == Status.REJECTED:
        return f"❌ Отклонена на этапе «{escape(wf.stage_title(req.stage_code))}»"
    if req.status == Status.ANNULLED:
        return "🗑 Аннулирована администратором"
    return "🚫 Отозвана автором"


def request_card(req: Request, wf: WorkflowConfig, tz: ZoneInfo, short_description: bool = False) -> str:
    description = req.description
    if short_description and len(description) > 500:
        description = description[:500] + "…"
    lines = [
        f"<b>Заявка №{req.id}</b> · {escape(wf.type_title(req.type_code))}",
        f"Сумма: <b>{money(req.amount, wf.currency)}</b>",
        f"Автор: {escape(req.author.display)}",
        f"Описание: {escape(description)}",
        f"Создана: {fmt_dt(req.created_at, tz)}",
        f"Статус: {status_line(req, wf)}",
    ]
    if req.close_reason:
        label = "Причина аннулирования" if req.status == Status.ANNULLED else "Причина отказа"
        lines.append(f"{label}: <i>{escape(req.close_reason)}</i>")
    return "\n".join(lines)


def timeline(req: Request, wf: WorkflowConfig, tz: ZoneInfo) -> str:
    lines = ["<b>История:</b>"]
    for ev in req.events:
        title = ACTION_TITLES.get(ev.action, ev.action)
        stage = f" «{escape(wf.stage_title(ev.stage_code))}»" if ev.stage_code else ""
        who = f" — {escape(ev.actor.full_name)}" if ev.actor else ""
        comment = f": <i>{escape(ev.comment)}</i>" if ev.comment else ""
        lines.append(f"• {fmt_dt(ev.created_at, tz, with_year=False)} {title}{stage}{who}{comment}")
    if req.is_open and req.stage_entered_at:
        waited = (utcnow() - req.stage_entered_at).total_seconds() / 3600
        lines.append(f"\n⏱ Ждёт решения уже {fmt_hours(waited)}")
    return "\n".join(lines)


def short_line(req: Request, wf: WorkflowConfig) -> str:
    icon = STATUS_ICONS.get(req.status, "")
    return (f"{icon} <b>№{req.id}</b> {escape(wf.type_title(req.type_code))} — "
            f"{money(req.amount, wf.currency)} · {escape(req.description[:40])}")


def welcome(user: User, open_access: bool) -> str:
    text = (
        f"Здравствуйте, {escape(user.full_name)}!\n\n"
        "Система согласования заявок на закупку, оплату и уценку.\n\n"
        "📝 <b>Новая заявка</b> — подать заявку\n"
        "📋 <b>Мои заявки</b> — статус ваших заявок\n"
        "📥 <b>Ждут моего решения</b> — заявки на согласование"
    )
    if open_access:
        text += (
            "\n\nВам доступны все роли маршрута: сотрудник, руководитель, директор, бухгалтер. "
            "Поданная заявка поступит вам на каждом этапе согласования. "
            "Свои заявки вы также можете исправлять как администратор: в карточке заявки "
            "есть кнопки «Вернуть на этап» и «Аннулировать»."
        )
    else:
        text += f"\n\nВаша роль: {user.role_title}" + (f", отдел: {escape(user.department)}" if user.department else "")
    return text


HELP = (
    "<b>Как это работает</b>\n"
    "1. Сотрудник подаёт заявку: тип, сумма, описание, фото или файл.\n"
    "2. Заявка идёт по маршруту: руководитель → (директор для крупных сумм) → бухгалтер.\n"
    "3. Каждый получает сообщение с кнопками. Отказ — только с причиной.\n"
    "4. Если решение не принято вовремя, бот напоминает.\n"
    "5. Всё пишется в журнал, а на дашборде видно, где заявки застревают.\n\n"
    "6. Ошибки исправляет администратор: аннулирует заявку или возвращает её на этап — "
    "тоже с причиной. Заявки не удаляются, история сохраняется.\n\n"
    "Команды: /new — новая заявка, /my — мои заявки, /inbox — ждут решения, "
    "/r 123 — открыть заявку по номеру, /id — мой Telegram ID"
)


def no_access(tg_id: int, username: str | None) -> str:
    who = f"@{escape(username)}" if username else "без ника"
    return (
        "У вас пока нет доступа к боту.\n\n"
        "Попросите администратора добавить вас в список сотрудников, указав:\n"
        f"Telegram: <code>{tg_id}</code> ({who})"
    )


def last_comment(req: Request, action: Action) -> str:
    return next((e.comment or "" for e in reversed(req.events) if e.action == action), "")


def outcome_for_author(action: Action, req: Request, wf: WorkflowConfig, stage_title: str | None) -> str:
    stage = f"«{escape(stage_title)}»" if stage_title else ""
    if action == Action.RETURNED:
        return (f"↩️ Заявка №{req.id} возвращена администратором на этап {stage}.\n"
                f"Причина: <i>{escape(last_comment(req, Action.RETURNED))}</i>\n"
                f"Сейчас: {status_line(req, wf)}")
    if action == Action.ANNULLED:
        return (f"🗑 Заявка №{req.id} аннулирована администратором.\n"
                f"Причина: <i>{escape(req.close_reason or '')}</i>")
    if action in (Action.APPROVED, Action.EXECUTED) and req.is_open:
        return (f"👍 Заявка №{req.id}: этап {stage} пройден.\n"
                f"Дальше: {status_line(req, wf)}")
    if req.status == Status.DONE:
        return f"🎉 Заявка №{req.id} исполнена!"
    if req.status == Status.REJECTED:
        return (f"❌ Заявка №{req.id} отклонена на этапе {stage}.\n"
                f"Причина: <i>{escape(req.reject_reason or '')}</i>")
    return f"Заявка №{req.id}: {status_line(req, wf)}"
