"""Справочник сотрудников: кто есть кто и в каком отделе.

Источник — лист «Сотрудники» в Google Таблице или файл config/staff.yaml.
Директор правит таблицу — бот через несколько минут сам подхватывает изменения.
"""
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Role, User

log = logging.getLogger(__name__)

ROLE_ALIASES = {
    "сотрудник": Role.EMPLOYEE, "employee": Role.EMPLOYEE, "": Role.EMPLOYEE,
    "руководитель": Role.MANAGER, "manager": Role.MANAGER,
    "бухгалтер": Role.ACCOUNTANT, "accountant": Role.ACCOUNTANT,
    "директор": Role.DIRECTOR, "director": Role.DIRECTOR,
    "администратор": Role.ADMIN, "admin": Role.ADMIN,
}
NO_VALUES = {"нет", "no", "false", "0", "уволен"}

# Как колонки называются в таблице / ключи в yaml
COLUMN_KEYS = {
    "telegram": ("telegram", "Telegram"),
    "name": ("name", "ФИО"),
    "department": ("department", "Отдел"),
    "role": ("role", "Роль"),
    "active": ("active", "Активен"),
}


@dataclass(frozen=True)
class StaffRecord:
    tg_id: int | None
    username: str | None
    full_name: str
    department: str | None
    role: Role
    active: bool


def _get(row: dict, key: str) -> str:
    for name in COLUMN_KEYS[key]:
        if name in row and row[name] is not None:
            return str(row[name]).strip()
    return ""


def parse_staff_rows(rows: list[dict]) -> tuple[list[StaffRecord], list[str]]:
    """Разбирает строки списка. Возвращает (записи, ошибки) — кривые строки пропускаются."""
    records: list[StaffRecord] = []
    errors: list[str] = []
    for n, row in enumerate(rows, start=1):
        telegram = _get(row, "telegram")
        name = _get(row, "name")
        if not telegram and not name:
            continue  # пустая строка
        if not telegram:
            errors.append(f"Строка {n}: не указан Telegram у «{name}»")
            continue
        role_raw = _get(row, "role").lower()
        role = ROLE_ALIASES.get(role_raw)
        if role is None:
            errors.append(f"Строка {n}: неизвестная роль «{role_raw}»")
            continue
        tg_id, username = None, None
        if telegram.lstrip("-").isdigit():
            tg_id = int(telegram)
        else:
            username = telegram.lstrip("@").lower()
        records.append(StaffRecord(
            tg_id=tg_id,
            username=username,
            full_name=name or telegram,
            department=_get(row, "department") or None,
            role=role,
            active=_get(row, "active").lower() not in NO_VALUES,
        ))
    return records, errors


def load_staff_yaml(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return list(data.get("staff") or [])


async def apply_staff(session: AsyncSession, records: list[StaffRecord]) -> dict[str, int]:
    """Синхронизирует пользователей в базе со списком сотрудников."""
    users = list((await session.execute(select(User))).scalars())
    by_tg = {u.tg_id: u for u in users if u.tg_id is not None}
    by_name = {u.username: u for u in users if u.username}
    seen: set[int] = set()
    stats = {"created": 0, "updated": 0, "deactivated": 0}

    for rec in records:
        user = (by_tg.get(rec.tg_id) if rec.tg_id else None) or (
            by_name.get(rec.username) if rec.username else None
        )
        if user is None:
            user = User(tg_id=rec.tg_id, username=rec.username, full_name=rec.full_name, source="staff")
            session.add(user)
            stats["created"] += 1
        else:
            stats["updated"] += 1
        user.full_name = rec.full_name
        user.department = rec.department
        user.role = rec.role
        user.is_active = rec.active
        user.source = "staff"
        await session.flush()
        seen.add(user.id)

    # Кого убрали из списка — отключаем (историю заявок не трогаем).
    # Если список пришёл пустым — скорее всего, его случайно стёрли: никого не отключаем.
    if not records:
        if any(u.source == "staff" and u.is_active for u in users):
            log.warning("Список сотрудников пуст — пропускаю отключение пользователей")
        return stats
    for user in users:
        if user.source == "staff" and user.id not in seen and user.is_active:
            user.is_active = False
            stats["deactivated"] += 1
    return stats


async def find_or_register(
    session: AsyncSession, tg_id: int, username: str | None, full_name: str, open_access: bool,
) -> User | None:
    """Находит пользователя Telegram в базе. В режиме открытого доступа регистрирует новых."""
    username = username.lower() if username else None
    user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
    if user is None and username:
        user = (await session.execute(
            select(User).where(User.username == username, User.tg_id.is_(None))
        )).scalar_one_or_none()
        if user is not None:
            user.tg_id = tg_id  # человек из списка впервые написал боту — привязываем
    if user is None and open_access:
        taken = username and (await session.execute(
            select(User.id).where(User.username == username)
        )).first()
        user = User(
            tg_id=tg_id, username=None if taken else username, full_name=full_name,
            department="Общий", role=Role.EMPLOYEE, source="self", is_active=True,
        )
        session.add(user)
    if user is None:
        return None
    await session.flush()
    return user if user.is_active else None
