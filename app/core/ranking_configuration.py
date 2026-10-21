"""Crew ranking settings an administrator can change: mission groups, weights, minimum flights."""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.core.kpi_configuration import normalize_kpi_text

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
GroupId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,39}$")]


class RankingWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # How much each criterion counts in the score; 0 leaves it out.
    avg_flights: float = Field(default=1, ge=0, le=10, allow_inf_nan=False)
    efficiency: float = Field(default=1, ge=0, le=10, allow_inf_nan=False)
    loss_rate: float = Field(default=1, ge=0, le=10, allow_inf_nan=False)

    @model_validator(mode="after")
    def some_weight(self):
        if not (self.avg_flights or self.efficiency or self.loss_rate):
            raise ValueError("At least one ranking criterion needs a weight above zero")
        return self


class MissionGroup(BaseModel):
    """Missions ranked together: crews are compared only with crews that flew the same kind of missions."""
    model_config = ConfigDict(extra="forbid")
    id: GroupId
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    purposes: list[Text] = Field(default_factory=list, max_length=200)
    # Кафедри whose devices do not come back by design (FPV strikes): losses say nothing there.
    ignore_losses_for: list[Text] = Field(default_factory=list, max_length=100)


OTHER_GROUP = ("other", "Інші місії")


def _defaults() -> list[MissionGroup]:
    return [
        MissionGroup(id="strike", title="Ударні", purposes=["Динамічне ураження", "Планове ураження", "Вільне полювання", "Засідка"], ignore_losses_for=["FPV", "Ударні крила"]),
        MissionGroup(id="logistics", title="Логістика", purposes=["Доставка", "Евакуація"]),
        MissionGroup(id="mining", title="Мінування", purposes=["Мінування"]),
        MissionGroup(id="recon", title="Розвідка", purposes=["Розвідка", "Дорозвідка", "Зйомка ураження"]),
    ]


class RankingConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # A crew is ranked in a mission group only with at least this many flights in it.
    min_flights: int = Field(default=5, ge=1, le=1000, strict=True)
    weights: RankingWeights = Field(default_factory=RankingWeights)
    # Missions of no group are ranked together as «Інші місії».
    mission_groups: list[MissionGroup] = Field(default_factory=_defaults, max_length=20)

    @model_validator(mode="after")
    def unique(self):
        ids = [group.id for group in self.mission_groups]
        if len(ids) != len(set(ids)) or OTHER_GROUP[0] in ids:
            raise ValueError("Mission group ids must be unique and not 'other'")
        purposes = [normalize_kpi_text(purpose) for group in self.mission_groups for purpose in group.purposes]
        if len(purposes) != len(set(purposes)):
            raise ValueError("A mission purpose can belong to one group only")
        return self

    def group_of(self) -> dict[str, tuple[str, str]]:
        """Normalized purpose → (group id, title)."""
        return {normalize_kpi_text(purpose): (group.id, group.title) for group in self.mission_groups for purpose in group.purposes}
