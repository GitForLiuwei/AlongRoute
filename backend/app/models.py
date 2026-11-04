from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class Point(BaseModel):
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)

    def csv(self) -> str:
        return f"{self.lon:.6f},{self.lat:.6f}"


class Place(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    point: Point
    citycode: str | None = None
    poi_id: str | None = None


class Preferences(BaseModel):
    max_detour_minutes: int = Field(default=5, ge=0, le=60)
    modes: list[Literal["taxi", "transit"]] = Field(default=["taxi", "transit"])
    priority: Literal["balanced", "time", "cost"] = "balanced"
    max_walking_m: int = Field(default=1200, ge=0, le=5000)
    max_transfers: int = Field(default=2, ge=0, le=8)

    @field_validator("modes")
    @classmethod
    def require_modes(cls, value):
        if not value:
            raise ValueError("至少选择一种后续交通方式")
        return list(dict.fromkeys(value))


class RecommendationRequest(BaseModel):
    origin: Place
    driver_destination: Place
    passenger_destination: Place
    preferences: Preferences = Field(default_factory=Preferences)
    departure_time: datetime | None = None

    @model_validator(mode="after")
    def not_identical(self):
        if self.origin.point == self.driver_destination.point:
            raise ValueError("司机起点和终点不能相同")
        return self


class RoadSegment(BaseModel):
    name: str = ""
    polyline: list[Point] = Field(default_factory=list)
    end_action: str = ""
    duration_s: int | None = None
    distance_m: int | None = None
    instruction: str = ""
    assistant_action: str = ""


class Route(BaseModel):
    duration_s: int = Field(ge=0)
    distance_m: int = Field(ge=0)
    polyline: list[Point] = Field(default_factory=list)
    fare_yuan: float | None = None
    is_schematic: bool = False
    segments: list[RoadSegment] = Field(default_factory=list, exclude=True)
    dropoff_duration_s: int | None = Field(default=None, exclude=True)
    walk_instructions: list[str] = Field(default_factory=list, exclude=True)


class TransitStep(BaseModel):
    kind: Literal["walk", "bus", "subway"]
    title: str
    distance_m: int | None = Field(default=None, ge=0)
    duration_s: int | None = Field(default=None, ge=0)
    from_stop: str | None = None
    to_stop: str | None = None
    line_name: str | None = None
    direction: str | None = None
    stop_count: int | None = Field(default=None, ge=0)
    via_stops: list[str] = Field(default_factory=list)
    entrance: str | None = None
    exit: str | None = None
    instructions: list[str] = Field(default_factory=list)


class ModeOption(BaseModel):
    mode: Literal["taxi", "transit"]
    duration_s: int
    fare_yuan: float | None = None
    walking_m: int | None = None
    transfers: int | None = None
    details: str
    route: list[Point] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    total_duration_s: int | None = Field(default=None, ge=0)
    arrival_at: datetime | None = None
    boarding_walk_m: int | None = Field(default=None, ge=0)
    boarding_stop: Place | None = None
    boarding_kind: Literal["bus", "subway"] | None = None
    boarding_walk_route: list[Point] = Field(default_factory=list)
    original_access_m: int | None = Field(default=None, exclude=True)
    original_access_s: int | None = Field(default=None, exclude=True)
    wait_estimate_s: int = 0
    transit_steps: list[TransitStep] = Field(default_factory=list)


class Recommendation(BaseModel):
    id: str
    point: Place
    driver_detour_s: int
    driver_route: Route
    shared_ride_s: int = Field(default=0, ge=0)
    stop_duration_s: int = Field(default=90, ge=0)
    candidate_source: Literal["roadside", "parking", "station_area"] = "parking"
    options: list[ModeOption]
    suggested_mode: Literal["taxi", "transit"]
    score: float
    stop_status: Literal["unverified", "verified"] = "unverified"
    stop_warning: str = "候选位置尚未验证能否合法安全停车，请勿直接在此下车。"
    reason: str
    driving_extra_s: int = 0
    comparison: str = ""
    highway_exit: Place | None = None
    original_exit: bool = False


class ComparisonOption(BaseModel):
    label: str
    options: list[ModeOption]


class RecommendationResponse(BaseModel):
    mode: Literal["demo", "live"]
    baseline_route: Route
    recommendations: list[Recommendation]
    warnings: list[str] = Field(default_factory=list)
    generated_at: datetime
    examined_candidates: int = 0
    station_walk_limit_m: Literal[300] = 300
    comparisons: list[ComparisonOption] = Field(default_factory=list)
    search_complete: bool = False
    departure_at: datetime | None = None
    expires_at: datetime | None = None


class PlaceSuggestion(BaseModel):
    name: str
    address: str = ""
    point: Point
    citycode: str | None = None
    poi_id: str | None = None
