import pytest
import pytest_asyncio
from sqlalchemy.pool import StaticPool

from app.config import BASE_DIR
from app.db import init_db, make_engine, make_sessionmaker
from app.models import Role, User
from app.services.approval import ApprovalService
from app.workflow_config import load_workflow


@pytest.fixture
def wf():
    return load_workflow(BASE_DIR / "config" / "workflow.yaml")


@pytest_asyncio.fixture
async def session():
    engine = make_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    await init_db(engine)
    sm = make_sessionmaker(engine)
    async with sm() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def service(wf):
    return ApprovalService(wf, open_access=False)


@pytest_asyncio.fixture
async def staff(session):
    people = {
        "employee": User(username="emp", full_name="Сотрудник", department="Продажи", role=Role.EMPLOYEE),
        "manager": User(username="man", full_name="Руководитель", department="Продажи", role=Role.MANAGER),
        "other_manager": User(username="man2", full_name="Чужой руководитель", department="Склад", role=Role.MANAGER),
        "director": User(username="dir", full_name="Директор", department="Дирекция", role=Role.DIRECTOR),
        "accountant": User(username="acc", full_name="Бухгалтер", department="Бухгалтерия", role=Role.ACCOUNTANT),
        "admin": User(username="adm", full_name="Администратор", department="Офис", role=Role.ADMIN),
    }
    session.add_all(people.values())
    await session.flush()
    return people
