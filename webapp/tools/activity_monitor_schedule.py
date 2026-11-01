"""Small, side-effect-free Activity Monitor due-date rules.

When a confirmed race is newer than an imported Arie Tool schedule snapshot,
the old milestone completions no longer prove a full search for that new race
basis. Rebuild the milestone dates from the new basis and keep all milestones
open until full-search evidence is available.
"""
import hashlib
import random
from datetime import date, datetime, time, timedelta


MILESTONES = (100, 150, 200, 250, 300, 400, 500)
WINTER_MILESTONES = {100, 150}
WINTER_START = (11, 1)
WINTER_END = (2, 1)
DEADLINE_DAYS = 5
FOLLOWUP_DAYS = 30
MIN_ATTEMPT_GAP_DAYS = 20
GENERAL_RETRY_DAYS = 20
FREE_AGENT_RETRY_DAYS = 30


def planned_rounds(day, window_start, window_end, rounds_per_hour):
    """Build Arie Tool-style stable daily random rounds in Amsterdam local time."""
    start_time = time.fromisoformat(window_start)
    end_time = time.fromisoformat(window_end)
    start = datetime.combine(day, start_time)
    end = datetime.combine(day, end_time)
    if end <= start:
        return []

    rounds_per_hour = max(1, min(5, int(rounds_per_hour)))
    seed = hashlib.sha256(
        f'{day.isoformat()}|{window_start}|{window_end}|{rounds_per_hour}'.encode()
    ).digest()
    rng = random.Random(seed)
    total_minutes = int((end - start).total_seconds() // 60)
    result = []
    previous = None
    elapsed = 0
    while elapsed < total_minutes:
        segment_minutes = min(60, total_minutes - elapsed)
        count = rounds_per_hour if segment_minutes == 60 else min(
            rounds_per_hour,
            segment_minutes // 10,
            int(rounds_per_hour * segment_minutes / 60 + 0.5),
        )
        segment_start = start + timedelta(minutes=elapsed)
        earliest = 0 if previous is None else max(
            0, int((previous + timedelta(minutes=10) - segment_start).total_seconds() // 60)
        )
        count = min(count, max(0, (segment_minutes - 1 - earliest) // 10 + 1))
        positions = list(range(earliest, segment_minutes))
        chosen = None
        for _ in range(200):
            trial = sorted(rng.sample(positions, count)) if count else []
            if all(right - left >= 10 for left, right in zip(trial, trial[1:])):
                chosen = trial
                break
        if chosen is None:
            chosen = [earliest + 10 * index for index in range(count)]
        for minute in chosen:
            planned = segment_start + timedelta(minutes=minute)
            if planned < end and (previous is None or planned - previous >= timedelta(minutes=10)):
                result.append(planned)
                previous = planned
        elapsed += segment_minutes
    return result


def next_planned_round(after, window_start, window_end, rounds_per_hour):
    """Return the next future round, rolling forward through the next day if needed."""
    for day_offset in range(0, 3):
        day = after.date() + timedelta(days=day_offset)
        for planned in planned_rounds(day, window_start, window_end, rounds_per_hour):
            if planned > after:
                return planned
    return None


def _is_winter(day):
    month_day = (day.month, day.day)
    return month_day >= WINTER_START or month_day <= WINTER_END


def _winter_start_between(start, end):
    for year in range(start.year, end.year + 1):
        boundary = date(year, *WINTER_START)
        if start < boundary <= end:
            return boundary
    return None


def due_from_new_activity(activity_date, as_of, team_id=None, full_research_date=None):
    """Return safe replacement plan fields after genuinely newer activity.

    A source result updates the inactivity basis only. It never marks research
    complete. Team 119 retains its separate 345-day free-agent trigger.
    """
    if not activity_date or activity_date <= date.min or activity_date > as_of:
        return None

    full_day = full_research_date
    if isinstance(full_day, str) and full_day:
        try:
            full_day = date.fromisoformat(full_day[:10])
        except ValueError:
            full_day = None
    if full_day and full_day > as_of:
        full_day = None

    candidates = []
    completed = []
    pending = []
    for threshold in MILESTONES:
        reached = activity_date + timedelta(days=threshold)
        deadline = reached + timedelta(days=DEADLINE_DAYS)
        winter_crossed = False
        if threshold in WINTER_MILESTONES:
            if _is_winter(reached):
                continue
            boundary = _winter_start_between(reached, deadline)
            if boundary:
                deadline = boundary - timedelta(days=1)
            winter_crossed = bool(full_day and _winter_start_between(reached, full_day))
            if _winter_start_between(reached, as_of) and not (
                full_day and full_day >= reached and not winter_crossed
            ):
                continue
        if full_day and full_day >= reached and not winter_crossed:
            completed.append(threshold)
            continue
        pending.append(threshold)
        candidates.append((reached, deadline, threshold))

    if team_id == 119:
        free_agent_due = activity_date + timedelta(days=345)
        candidates.append((free_agent_due, free_agent_due, 'Free-agent 345-day review'))

    if candidates:
        due, deadline, milestone = min(candidates, key=lambda item: (item[0], item[1]))
        return {
            'next_research_date': due.isoformat(),
            'research_deadline': deadline.isoformat(),
            'research_reason': (
                f'{milestone}-day review after confirmed activity'
                if isinstance(milestone, int)
                else milestone
            ),
            'completed_milestones': ';'.join(map(str, completed)),
            'pending_milestones': ';'.join(
                str(value) for value in pending
                if activity_date + timedelta(days=value) <= as_of
            ),
            'plan_recalculated': True,
        }

    due = activity_date + timedelta(days=MILESTONES[-1] + FOLLOWUP_DAYS)
    return {
        'next_research_date': due.isoformat(),
        'research_deadline': (due + timedelta(days=DEADLINE_DAYS)).isoformat(),
        'research_reason': 'New confirmed activity; human follow-up after 500-day milestone',
        'completed_milestones': ';'.join(map(str, completed)),
        'pending_milestones': ';'.join(
            str(value) for value in pending
            if activity_date + timedelta(days=value) <= as_of
        ),
        'plan_recalculated': True,
    }


def apply_attempt_gates(plan, as_of, team_id=None, latest_attempt=None,
                        latest_cycle=None, latest_activity=None):
    """Keep previous research/source attempts inside the Arie Tool cooldowns."""
    if not plan.get('next_research_date'):
        return plan

    due = date.fromisoformat(str(plan['next_research_date'])[:10])
    deadline = date.fromisoformat(str(plan.get('research_deadline') or plan['next_research_date'])[:10])
    attempt_day = latest_attempt.get('date') if latest_attempt else None
    if attempt_day:
        due = max(due, attempt_day + timedelta(days=MIN_ATTEMPT_GAP_DAYS))

    cycle_day = latest_cycle.get('date') if latest_cycle else None
    if cycle_day:
        cycle_basis = latest_cycle.get('latest_participation_date')
        if isinstance(cycle_basis, str) and cycle_basis:
            try:
                cycle_basis = date.fromisoformat(cycle_basis[:10])
            except ValueError:
                cycle_basis = None
        no_newer_activity = (
            latest_activity is None
            or (cycle_basis is not None and latest_activity <= cycle_basis)
        )
        if cycle_day <= as_of and no_newer_activity:
            retry_days = FREE_AGENT_RETRY_DAYS if team_id == 119 else GENERAL_RETRY_DAYS
            due = max(due, cycle_day + timedelta(days=retry_days))

    plan['next_research_date'] = due.isoformat()
    plan['research_deadline'] = max(deadline, due).isoformat()
    plan['days_until_due'] = max(0, (due - as_of).days)
    return plan
