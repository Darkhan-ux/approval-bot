"""Аналитика для дашборда: сколько заявок, сколько одобрено, где застревают."""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import mean, median
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.formatting import local_dt
from app.models import DECISION_ACTIONS, Action, Request, Status, utcnow
from app.workflow_config import WorkflowConfig


def _hours(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 3600


@dataclass
class StageStat:
    code: str
    title: str
    passed: int = 0
    rejected: int = 0
    avg_h: float | None = None
    median_h: float | None = None
    pending_now: int = 0
    pending_avg_wait_h: float | None = None


@dataclass
class PersonStat:
    name: str
    role: str
    department: str | None
    decisions: int = 0
    avg_h: float | None = None
    pending_now: int = 0
    oldest_wait_h: float | None = None


@dataclass
class StuckItem:
    id: int
    type_title: str
    amount: Decimal
    author: str
    stage_title: str
    assignees: str
    wait_h: float
    overdue: bool


@dataclass
class DayPoint:
    day: date
    created: int
    closed: int


@dataclass
class Stats:
    days: int
    total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    approval_rate: float | None = None
    avg_cycle_h: float | None = None
    median_cycle_h: float | None = None
    total_amount_done: Decimal = Decimal(0)
    reminders: int = 0
    open_now: int = 0
    overdue_now: int = 0
    stages: list[StageStat] = field(default_factory=list)
    people: list[PersonStat] = field(default_factory=list)
    stuck: list[StuckItem] = field(default_factory=list)
    daily: list[DayPoint] = field(default_factory=list)
    by_type: list[tuple[str, int, Decimal]] = field(default_factory=list)
    bottleneck_stage: StageStat | None = None
    slowest_person: PersonStat | None = None


async def compute_stats(session: AsyncSession, wf: WorkflowConfig, tz: ZoneInfo, days: int = 30,
                        now: datetime | None = None) -> Stats:
    now = now or utcnow()
    since = now - timedelta(days=days)
    # Заявки за период + все открытые (даже если созданы раньше — они и есть «застрявшие»)
    requests = list((await session.execute(
        select(Request).where(or_(Request.created_at >= since, Request.status == Status.IN_PROGRESS))
        .order_by(Request.id)
    )).scalars())
    # Аннулированные заявки созданы по ошибке — в цифрах их не учитываем
    requests = [r for r in requests if r.status != Status.ANNULLED]
    in_period = [r for r in requests if r.created_at >= since]

    s = Stats(days=days, total=len(in_period))
    for st in Status:
        s.by_status[st] = sum(1 for r in in_period if r.status == st)
    decided = s.by_status[Status.DONE] + s.by_status[Status.REJECTED]
    if decided:
        s.approval_rate = s.by_status[Status.DONE] / decided

    cycles = [_hours(r.created_at, r.closed_at) for r in in_period if r.status == Status.DONE and r.closed_at]
    if cycles:
        s.avg_cycle_h, s.median_cycle_h = mean(cycles), median(cycles)
    s.total_amount_done = sum((r.amount for r in in_period if r.status == Status.DONE), Decimal(0))

    # --- время на каждом этапе и у каждого человека ---
    stage_durations: dict[str, list[float]] = defaultdict(list)
    stage_rejected: dict[str, int] = defaultdict(int)
    person_durations: dict[int, list[float]] = defaultdict(list)
    people: dict[int, PersonStat] = {}

    def person(user) -> PersonStat:
        if user.id not in people:
            people[user.id] = PersonStat(user.full_name, user.role_title, user.department)
        return people[user.id]

    for r in in_period:
        opened: dict[int, datetime] = {}
        for ev in r.events:
            if ev.action == Action.REMINDED:
                s.reminders += 1
            if ev.action == Action.ENTERED and ev.stage_index is not None:
                opened[ev.stage_index] = ev.created_at
            elif ev.action in DECISION_ACTIONS and ev.stage_index in opened:
                h = _hours(opened.pop(ev.stage_index), ev.created_at)
                stage_durations[ev.stage_code].append(h)
                if ev.action == Action.REJECTED:
                    stage_rejected[ev.stage_code] += 1
                if ev.actor:
                    person(ev.actor).decisions += 1
                    person_durations[ev.actor.id].append(h)

    # --- что висит прямо сейчас ---
    open_now = [r for r in requests if r.is_open and r.stage_entered_at]
    s.open_now = len(open_now)
    pending_wait: dict[str, list[float]] = defaultdict(list)
    for r in open_now:
        wait = _hours(r.stage_entered_at, now)
        pending_wait[r.stage_code].append(wait)
        overdue = wait >= wf.reminder_after_hours
        s.overdue_now += overdue
        assignees = r.current_assignees()
        for u in assignees:
            p = person(u)
            p.pending_now += 1
            p.oldest_wait_h = max(p.oldest_wait_h or 0, wait)
        s.stuck.append(StuckItem(
            id=r.id, type_title=wf.type_title(r.type_code), amount=r.amount,
            author=r.author.full_name, stage_title=wf.stage_title(r.stage_code),
            assignees=", ".join(u.full_name for u in assignees), wait_h=wait, overdue=overdue,
        ))
    s.stuck.sort(key=lambda x: x.wait_h, reverse=True)
    s.stuck = s.stuck[:10]

    for stage in wf.stages:
        d = stage_durations.get(stage.code, [])
        w = pending_wait.get(stage.code, [])
        s.stages.append(StageStat(
            code=stage.code, title=stage.title,
            passed=len(d) - stage_rejected.get(stage.code, 0), rejected=stage_rejected.get(stage.code, 0),
            avg_h=mean(d) if d else None, median_h=median(d) if d else None,
            pending_now=len(w), pending_avg_wait_h=mean(w) if w else None,
        ))

    for uid, p in people.items():
        d = person_durations.get(uid)
        p.avg_h = mean(d) if d else None
    s.people = sorted(people.values(), key=lambda p: (p.avg_h or 0), reverse=True)

    # Узкое место — этап с наибольшим средним временем; самый медленный — среди тех, у кого ≥3 решений
    with_time = [st for st in s.stages if st.avg_h is not None]
    s.bottleneck_stage = max(with_time, key=lambda st: st.avg_h, default=None)
    candidates = [p for p in s.people if p.avg_h is not None and p.decisions >= 3]
    s.slowest_person = max(candidates, key=lambda p: p.avg_h, default=None)

    # --- по дням (в местном времени) ---
    today = local_dt(now, tz).date()
    created_by_day: dict[date, int] = defaultdict(int)
    closed_by_day: dict[date, int] = defaultdict(int)
    for r in in_period:
        created_by_day[local_dt(r.created_at, tz).date()] += 1
    for r in requests:
        if r.closed_at and r.closed_at >= since:
            closed_by_day[local_dt(r.closed_at, tz).date()] += 1
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        s.daily.append(DayPoint(d, created_by_day.get(d, 0), closed_by_day.get(d, 0)))

    # --- по типам ---
    types: dict[str, list] = {}
    for r in in_period:
        t = types.setdefault(r.type_code, [0, Decimal(0)])
        t[0] += 1
        t[1] += r.amount
    s.by_type = sorted(((wf.type_title(c), n, total) for c, (n, total) in types.items()),
                       key=lambda x: x[1], reverse=True)
    return s


async def recent_requests(session: AsyncSession, limit: int = 15) -> list[Request]:
    return list((await session.execute(
        select(Request).order_by(Request.created_at.desc()).limit(limit)
    )).scalars())
