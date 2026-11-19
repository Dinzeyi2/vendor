"""Timefold Solver: school timetabling (the solver's reference planning problem).

Assigns each lesson a timeslot and a room.
Hard: no room, teacher or student-group double-booking. Soft: teachers keep one room.
Documented API: @planning_entity / @planning_solution dataclasses + @constraint_provider,
SolverFactory.create(SolverConfig(...)).build_solver().solve(problem). Needs JDK 17+ (JAVA_HOME).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Dict, List, Optional

from timefold.solver import SolverFactory
from timefold.solver.config import (Duration, ScoreDirectorFactoryConfig, SolverConfig,
                                    TerminationConfig)
from timefold.solver.domain import (PlanningEntityCollectionProperty, PlanningId, PlanningScore,
                                    PlanningVariable, ProblemFactCollectionProperty,
                                    ValueRangeProvider, planning_entity, planning_solution)
from timefold.solver.score import (Constraint, ConstraintFactory, HardSoftScore, Joiners,
                                   constraint_provider)

from app.errors import OperationError
from app.ops._validate import number, require


@dataclass
class Timeslot:
    id: Annotated[str, PlanningId]
    day_of_week: str
    start_time: str
    end_time: str


@dataclass
class Room:
    id: Annotated[str, PlanningId]
    name: str


@planning_entity
@dataclass
class Lesson:
    id: Annotated[str, PlanningId]
    subject: str
    teacher: str
    student_group: str
    timeslot: Annotated[Optional[Timeslot], PlanningVariable] = field(default=None)
    room: Annotated[Optional[Room], PlanningVariable] = field(default=None)


@planning_solution
@dataclass
class Timetable:
    timeslots: Annotated[List[Timeslot], ProblemFactCollectionProperty, ValueRangeProvider]
    rooms: Annotated[List[Room], ProblemFactCollectionProperty, ValueRangeProvider]
    lessons: Annotated[List[Lesson], PlanningEntityCollectionProperty]
    score: Annotated[Optional[HardSoftScore], PlanningScore] = field(default=None)


@constraint_provider
def define_constraints(f: ConstraintFactory) -> List[Constraint]:
    return [
        f.for_each_unique_pair(Lesson, Joiners.equal(lambda l: l.timeslot), Joiners.equal(lambda l: l.room))
         .penalize(HardSoftScore.ONE_HARD).as_constraint("Room conflict"),
        f.for_each_unique_pair(Lesson, Joiners.equal(lambda l: l.timeslot), Joiners.equal(lambda l: l.teacher))
         .penalize(HardSoftScore.ONE_HARD).as_constraint("Teacher conflict"),
        f.for_each_unique_pair(Lesson, Joiners.equal(lambda l: l.timeslot), Joiners.equal(lambda l: l.student_group))
         .penalize(HardSoftScore.ONE_HARD).as_constraint("Student group conflict"),
        f.for_each_unique_pair(Lesson, Joiners.equal(lambda l: l.teacher))
         .filter(lambda a, b: a.room != b.room)
         .penalize(HardSoftScore.ONE_SOFT).as_constraint("Teacher room stability"),
    ]


def _records(inputs: Dict[str, Any], key: str, fields: List[str], limit: int) -> List[Dict[str, str]]:
    items = require(inputs, key)
    if not isinstance(items, list) or not items:
        raise OperationError(f"input '{key}' must be a non-empty list")
    if len(items) > limit:
        raise OperationError(f"input '{key}' has more than {limit} items")
    out = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or any(not isinstance(item.get(k), str) for k in fields):
            raise OperationError(f"{key}[{i}] must have string fields {fields}")
        out.append({k: item[k] for k in fields})
    if len({r["id"] for r in out}) != len(out):
        raise OperationError(f"ids in '{key}' must be unique")
    return out


def school_timetabling(inputs: Dict[str, Any]) -> Dict[str, Any]:
    timeslots = [Timeslot(**t) for t in _records(inputs, "timeslots", ["id", "day_of_week", "start_time", "end_time"], 500)]
    rooms = [Room(**r) for r in _records(inputs, "rooms", ["id", "name"], 200)]
    lessons = [Lesson(**l) for l in _records(inputs, "lessons", ["id", "subject", "teacher", "student_group"], 2000)]
    seconds = number(inputs, "time_limit_seconds", 5, 1, 120)

    config = SolverConfig(
        solution_class=Timetable, entity_class_list=[Lesson],
        score_director_factory_config=ScoreDirectorFactoryConfig(constraint_provider_function=define_constraints),
        termination_config=TerminationConfig(spent_limit=Duration(milliseconds=int(seconds * 1000)),
                                             best_score_limit="0hard/0soft"),
    )
    solution = SolverFactory.create(config).build_solver().solve(Timetable(timeslots, rooms, lessons))
    score = solution.score
    return {
        "score": str(score),
        "hard_score": score.hard_score,
        "soft_score": score.soft_score,
        "feasible": score.is_feasible,
        "assignments": [
            {"lesson_id": l.id, "timeslot_id": l.timeslot.id if l.timeslot else None,
             "room_id": l.room.id if l.room else None}
            for l in solution.lessons
        ],
    }


OPERATIONS = {"timefold.school_timetabling": school_timetabling}
