"""Отказ по умолчанию: этап без ответственного не пропускается."""
from decimal import Decimal

from app.models import Action, Status
from app.services.approval import UNASSIGNED_NOTE


async def create(service, session, author, amount):
    out = await service.create(session, author, "payment", Decimal(amount), "Оплата поставщику")
    return out


async def test_no_accountant_request_does_not_become_done(service, session, staff):
    """Бухгалтер уволился: после одобрения руководителя заявка НЕ становится «Исполнена»."""
    staff["accountant"].is_active = False
    await session.flush()
    req = (await create(service, session, staff["employee"], "15000")).request

    out = await service.approve(session, req.id, req.stage_index, staff["manager"])
    assert req.status == Status.IN_PROGRESS
    assert req.stage_code == "accountant"
    assert req.unassigned
    assert [u.username for u in out.new_assignees] == ["adm"]  # решение временно у администратора
    assert any(e.action == Action.ENTERED and e.comment == UNASSIGNED_NOTE for e in req.events)
    assert not any(e.action == Action.SKIPPED and e.stage_code == "accountant" for e in req.events)


async def test_no_manager_and_no_director_large_amount_waits(service, session, staff):
    """Нет руководителя отдела, директор неактивен: 900 000 ₸ НЕ уходит сразу бухгалтеру."""
    staff["manager"].is_active = False
    staff["director"].is_active = False
    await session.flush()
    out = await create(service, session, staff["employee"], "900000")
    req = out.request
    assert req.status == Status.IN_PROGRESS
    assert req.stage_code == "manager"
    assert req.unassigned
    assert [u.username for u in out.new_assignees] == ["adm"]
    assert all(a.user.username != "acc" for a in req.assignments)


async def test_stays_on_stage_even_without_admins(service, session, staff):
    """Даже если и администраторов нет — заявка ждёт, а не проходит дальше."""
    staff["accountant"].is_active = False
    staff["admin"].is_active = False
    await session.flush()
    req = (await create(service, session, staff["employee"], "15000")).request
    out = await service.approve(session, req.id, req.stage_index, staff["manager"])
    assert req.status == Status.IN_PROGRESS and req.stage_code == "accountant"
    assert out.new_assignees == [] and req.current_assignees() == []


async def test_threshold_skip_still_works(service, session, staff):
    """Пропуск по порогу суммы — единственный законный пропуск этапа."""
    req = (await create(service, session, staff["employee"], "15000")).request
    await service.approve(session, req.id, req.stage_index, staff["manager"])
    assert req.stage_code == "accountant" and not req.unassigned
    assert any(e.action == Action.SKIPPED and e.stage_code == "director" for e in req.events)


async def test_admin_reassigns_after_hiring(service, session, staff):
    """Наняли бухгалтера → администратор «возвращает» заявку на тот же этап, и она уходит бухгалтеру."""
    staff["accountant"].is_active = False
    await session.flush()
    req = (await create(service, session, staff["employee"], "15000")).request
    await service.approve(session, req.id, req.stage_index, staff["manager"])
    assert "accountant" in [s.code for s in service.returnable_stages(req)]

    staff["accountant"].is_active = True
    await session.flush()
    out = await service.return_to_stage(session, req.id, "accountant", staff["admin"], "Бухгалтер назначен")
    assert [u.username for u in out.new_assignees] == ["acc"]
    assert [u.username for u in req.current_assignees()] == ["acc"]  # администратор больше не в списке
    assert not req.unassigned
