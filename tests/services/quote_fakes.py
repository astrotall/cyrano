from __future__ import annotations

import random
from datetime import datetime, timedelta

from cyrano.domain.enums import PlanReason
from cyrano.domain.schedule import SendPlan, UnavailableSoon
from cyrano.services.humanization import HumanizationPlanner
from tests.services.autopilot_rig import AutopilotRig

LATE = 300
FIXED_PLANNER_SEED = 3


class FixedPlanner(HumanizationPlanner):
    def __init__(
        self,
        rig: AutopilotRig,
        *,
        delay_seconds: float = 0,
        late_seconds: int = LATE,
        soon: UnavailableSoon | None = None,
    ) -> None:
        super().__init__(rig.messages, rig.schedule, random.Random(FIXED_PLANNER_SEED))
        self.delay_seconds = delay_seconds
        self.late_seconds = late_seconds
        self.soon = soon
        self.late_reads = 0

    async def plan(
        self,
        contact_id: int,
        trigger_message_id: int,
        now: datetime,
        *,
        ongoing: bool | None = None,
        wrapup: UnavailableSoon | None = None,
        first_part: str = "",
    ) -> SendPlan:
        return SendPlan(
            send_at=now + timedelta(seconds=self.delay_seconds), reason=PlanReason.DELAYED
        )

    async def quote_late_seconds(self) -> int:
        self.late_reads += 1
        return self.late_seconds

    async def unavailable_soon(self, now: datetime) -> UnavailableSoon | None:
        return self.soon
