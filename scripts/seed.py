"""Заполняет базу правдоподобными тестовыми заявками за последние 45 дней,
чтобы дашборд не был пустым.

Заявки проходят через настоящую логику (ApprovalService), просто «в прошлом».

Запуск:  python -m scripts.seed          (добавить к существующим)
         python -m scripts.seed --reset  (сначала удалить все заявки)
"""
import argparse
import asyncio
import random
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import delete, select

from app.config import get_settings
from app.db import init_db, make_engine, make_sessionmaker
from app.models import Assignment, Request, RequestEvent, Role, User, utcnow
from app.services.approval import ApprovalService
from app.workflow_config import load_workflow

# (ник, имя, отдел, роль, среднее время реакции в часах)
PEOPLE = [
    ("e_seitkali", "Ерлан Сейткали", "Продажи", Role.MANAGER, 4),
    ("g_akhmetova", "Гульнара Ахметова", "Склад", Role.MANAGER, 30),
    ("a_nurpeisov", "Арман Нурпеисов", "Маркетинг", Role.MANAGER, 9),
    ("n_zhumabekov", "Нурлан Жумабеков", "Дирекция", Role.DIRECTOR, 12),
    ("a_nurlanova", "Айгерим Нурланова", "Бухгалтерия", Role.ACCOUNTANT, 7),
    ("a_kasymova", "Алия Касымова", "Продажи", Role.EMPLOYEE, 0),
    ("d_abenov", "Данияр Абенов", "Продажи", Role.EMPLOYEE, 0),
    ("zh_ospanova", "Жанар Оспанова", "Склад", Role.EMPLOYEE, 0),
    ("b_tleuov", "Бауыржан Тлеуов", "Склад", Role.EMPLOYEE, 0),
    ("a_muratova", "Асель Муратова", "Склад", Role.EMPLOYEE, 0),
    ("d_bekova", "Динара Бекова", "Маркетинг", Role.EMPLOYEE, 0),
    ("m_saparov", "Мади Сапаров", "Маркетинг", Role.EMPLOYEE, 0),
]

# (описание, минимальная и максимальная сумма в тенге)
DESCRIPTIONS = {
    "purchase": [
        ("Картриджи для принтера в офис", 20_000, 75_000), ("Стеллажи на склад", 200_000, 900_000),
        ("Ноутбук для нового менеджера", 350_000, 700_000), ("Канцелярия на квартал", 15_000, 60_000),
        ("Упаковочные материалы", 40_000, 225_000), ("Сканер штрихкодов", 45_000, 125_000),
    ],
    "payment": [
        ("Счёт за рекламу в Instagram и 2ГИС", 100_000, 600_000), ("Оплата доставки поставщику", 25_000, 150_000),
        ("Аренда стенда на выставке в Астане", 300_000, 1_250_000), ("Подписка на CRM", 75_000, 200_000),
        ("Счёт за ремонт погрузчика", 125_000, 450_000),
    ],
    "markdown": [
        ("Уценка товара с повреждённой упаковкой", 10_000, 100_000),
        ("Уценка остатков прошлой коллекции", 50_000, 300_000),
        ("Списание брака от поставщика", 15_000, 125_000),
    ],
}
REJECT_REASONS = [
    "Нет в бюджете этого месяца", "Нужен счёт от поставщика", "Дорого — найдите 2–3 альтернативы",
    "Согласуйте сначала с отделом закупок", "Не хватает обоснования",
]


def reaction(mean_h: float) -> timedelta:
    """Случайное время реакции: чаще быстро, иногда очень долго (как в жизни)."""
    return timedelta(hours=max(0.1, random.lognormvariate(0, 0.8) * mean_h * 0.75))


async def seed(reset: bool, count: int) -> None:
    settings = get_settings()
    wf = load_workflow(settings.workflow_file)
    engine = make_engine(settings.database_url)
    await init_db(engine)
    sm = make_sessionmaker(engine)
    service = ApprovalService(wf, open_access=False)  # настоящий маршрут по отделам
    random.seed(42)

    async with sm() as session:
        if reset:
            await session.execute(delete(RequestEvent))
            await session.execute(delete(Assignment))
            await session.execute(delete(Request))
            await session.execute(delete(User).where(User.source == "seed"))

        users: dict[str, User] = {}
        speed: dict[int, float] = {}
        for username, name, dept, role, mean_h in PEOPLE:
            user = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
            if user is None:
                user = User(username=username, full_name=name, department=dept, role=role, source="seed")
                session.add(user)
                await session.flush()
            users[username] = user
            speed[user.id] = mean_h
        employees = [u for u in users.values() if u.role == Role.EMPLOYEE]

        now = utcnow()
        # Время подачи: последние 45 дней, рабочие часы 9:00–18:00 по Алматы (UTC+5).
        # Сортируем заранее, чтобы номера заявок шли по порядку дат, как в жизни.
        moments = []
        for _ in range(count):
            t = now - timedelta(days=random.uniform(0, 45))
            t = t.replace(hour=random.randint(4, 12), minute=random.randint(0, 59))
            moments.append(t - timedelta(days=1) if t > now else t)
        moments.sort()

        created = 0
        for t in moments:
            author = random.choice(employees)
            type_code = random.choices(["purchase", "payment", "markdown"], weights=[5, 3, 2])[0]
            text, lo, hi = random.choice(DESCRIPTIONS[type_code])
            amount = Decimal(random.randrange(lo, hi, 1000))

            outcome = await service.create(session, author, type_code, amount, text, now=t)
            req = outcome.request
            created += 1

            # Проводим заявку по маршруту, пока не закончится или не «догонит» сегодняшний день
            while req.is_open:
                stage_index = req.stage_index
                assignee = random.choice(req.current_assignees())
                t = t + reaction(speed[assignee.id])
                if t > now:
                    break  # решения ещё нет — заявка висит
                if random.random() < (0.12 if wf.stages[stage_index].kind == "approve" else 0.04):
                    await service.reject(session, req.id, stage_index, assignee,
                                         random.choice(REJECT_REASONS), now=t)
                else:
                    await service.approve(session, req.id, stage_index, assignee, now=t)

            # Некоторые заявки автор отзывает
            if req.is_open and random.random() < 0.05:
                await service.cancel(session, req.id, author, now=min(now, t))

        # Напоминания по тем, что висят слишком долго
        for req in await service.overdue(session, now=now):
            service.mark_reminded(req, now=now)

        await session.commit()
    await engine.dispose()
    print(f"Готово: добавлено {created} тестовых заявок. Запустите приложение и откройте дашборд.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Тестовые данные для дашборда")
    parser.add_argument("--reset", action="store_true", help="удалить все заявки перед заполнением")
    parser.add_argument("--count", type=int, default=160, help="сколько заявок создать")
    args = parser.parse_args()
    asyncio.run(seed(args.reset, args.count))


if __name__ == "__main__":
    main()
