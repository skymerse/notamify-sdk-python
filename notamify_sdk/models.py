from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NotamifyModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# Watcher models
class ListenerMetadata(NotamifyModel):
    notams_shipped: int = 0


class ListenerTimeWindowFilter(NotamifyModel):
    days: list[str] | None = None
    start: str | None = None
    end: str | None = None


class ListenerAffectedElementFilter(NotamifyModel):
    effect: str | None = None
    type: str | None = None


class ListenerFilters(NotamifyModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    notam_icao: list[str] | None = None
    notam_type: list[str] | None = None
    airport_type: list[str] | None = None
    category: list[str] | None = None
    subcategory: list[str] | None = None
    qcode: list[str] | None = None
    time_windows: list[ListenerTimeWindowFilter] | None = None
    affected_element: list[ListenerAffectedElementFilter] | None = None


class ListenerMode(str, Enum):
    prod = "prod"
    sandbox = "sandbox"


class ListenerRequest(NotamifyModel):
    name: str | None = None
    webhook_url: str | None = None
    email: str | None = None
    filters: ListenerFilters = Field(default_factory=ListenerFilters)
    active: bool | None = None
    mode: ListenerMode | None = None
    lifecycle_enabled: bool | None = None


class Listener(NotamifyModel):
    id: str = ""
    name: str = ""
    webhook_url: str = ""
    email: str = ""
    filters: ListenerFilters = Field(default_factory=ListenerFilters)
    metadata: ListenerMetadata = Field(default_factory=ListenerMetadata)
    active: bool = True
    mode: ListenerMode = ListenerMode.prod
    lifecycle_enabled: bool = False
    webhook_secret: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Listener":
        return cls.model_validate(payload or {})


class WebhookLog(NotamifyModel):
    listener_id: str = ""
    webhook_url: str = ""
    notam_id: str = ""
    status: str = ""
    error: str = ""
    created_at: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WebhookLog":
        return cls.model_validate(payload or {})


class WebhookLogsResponse(NotamifyModel):
    success: list[WebhookLog] = Field(default_factory=list)
    errors: list[WebhookLog] = Field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WebhookLogsResponse":
        return cls.model_validate(payload or {})


class SandboxDeliveryResult(NotamifyModel):
    listener_id: str = ""
    mode: ListenerMode = ListenerMode.sandbox
    notam_id: str = ""
    sent_at: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SandboxDeliveryResult":
        return cls.model_validate(payload or {})


# Public API v2 NOTAM models
class LocationType(str, Enum):
    origin = "origin"
    destination = "destination"
    alternate = "alternate"
    fir = "fir"


class BriefingStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class NotamPriority(str, Enum):
    none = "NONE"
    high = "HIGH"
    medium = "MEDIUM"
    low = "LOW"


class AffectedElementDTO(NotamifyModel):
    type: str
    identifier: str
    effect: str
    details: str | None = None


class NotamScheduleInterpretationDTO(NotamifyModel):
    source: str
    description: str
    rrule: str
    duration_hrs: float | None = None
    is_sunrise_sunset: bool | None = None


class NotamMapElementDTO(NotamifyModel):
    element_type: str
    coordinates: list[dict[str, Any]]
    description: str
    area_number: str | None = None
    geojson: dict[str, Any] | None = None
    bottom: dict[str, Any] | None = None
    top: dict[str, Any] | None = None


class NotamInterpretationDTO(NotamifyModel):
    description: str
    excerpt: str
    category: str
    subcategory: str
    map_elements: list[NotamMapElementDTO] | None = None
    affected_elements: list[AffectedElementDTO] = Field(default_factory=list)
    schedules: list[NotamScheduleInterpretationDTO] = Field(default_factory=list)
    schedule_description: str | None = None


class NotamDTO(NotamifyModel):
    id: str
    notam_number: str
    location: str
    starts_at: datetime
    ends_at: datetime
    issued_at: datetime
    is_estimated: bool
    is_permanent: bool
    message: str
    notam_type: str | None = None
    icao_code: str | None = None
    classification: str | None = None
    icao_message: str | None = None
    qcode: str | None = None
    interpretation: NotamInterpretationDTO | None = None


class NotamListResult(NotamifyModel):
    notams: list[NotamDTO]
    total_count: int
    page: int
    per_page: int


class AircraftDetails(NotamifyModel):
    equipment: list[str] | None = None
    surveillance: list[str] | None = None
    adsb: list[str] | None = None
    adsc: list[str] | None = None
    faa_domestic: str | None = None
    fuel_type: str | None = None
    persons_on_board: int | None = None
    pbn_levels: str | None = None
    nav_augmentations: str | None = None
    other_information: str | None = None
    cwt: str | None = None
    mtow_kg: int | None = None
    mtow_lb: int | None = None
    num_engines: int | None = None
    wingspan_m: float | None = None
    wingspan_ft: float | None = None
    physical_class_engine: str | None = None


class LocationWithType(NotamifyModel):
    location: str
    starts_at: datetime
    ends_at: datetime
    type: LocationType | None = None
    always_include_est: bool = True
    excluded_classifications: list[str] | None = None


class GenerateFlightBriefingRequest(NotamifyModel):
    locations: list[LocationWithType]
    origin_runway: str | None = None
    destination_runway: str | None = None
    destination_procedure: str | None = None
    aircraft_type: str | None = None
    aircraft_details: AircraftDetails | None = None


class NotamPrioritisationRequest(GenerateFlightBriefingRequest):
    notam_id: str


class CriticalOperationalRestrictionGroup(NotamifyModel):
    location_code: str
    location_role: LocationType | None = None
    items: list[str] = Field(default_factory=list)


class BriefingResponse(NotamifyModel):
    text: str
    critical_operational_restrictions: list[CriticalOperationalRestrictionGroup] = Field(default_factory=list)
    highlights: list[str] | None = None


class GenerateFlightBriefingResponse(NotamifyModel):
    locations: list[LocationWithType]
    briefing: BriefingResponse


class BriefingJobCreated(NotamifyModel):
    uuid: str = ""
    status_url: str = ""


class BriefingJobStatusDTO(NotamifyModel):
    uuid: str
    status: BriefingStatus
    created_at: datetime
    updated_at: datetime
    response: GenerateFlightBriefingResponse | None = None


class PrioritizedNotamDTO(NotamifyModel):
    notam: NotamDTO
    priority: NotamPriority
    explanation: str
    considerations: list[str] = Field(default_factory=list)


class NotamPrioritisationResult(NotamifyModel):
    notams: list[PrioritizedNotamDTO]
    total_count: int
    page: int
    per_page: int


class WebhookMessageKind(str, Enum):
    interpretation = "interpretation"
    lifecycle = "lifecycle"


class WebhookLocationCoordinates(NotamifyModel):
    lat: float
    lon: float


class WebhookLocationContext(NotamifyModel):
    ident: str | None = None
    icao: str | None = None
    iata_code: str | None = None
    name: str | None = None
    iso_country: str | None = None
    iso_country_name: str | None = None
    elevation_ft: int | None = None
    coordinates: WebhookLocationCoordinates | None = None
    fir_icaos: list[str] = Field(default_factory=list)


class WebhookContext(NotamifyModel):
    location: WebhookLocationContext | None = None


class WebhookLifecycleChange(NotamifyModel):
    changed_notam_id: str
    notam_type: str


class WatcherWebhookEvent(NotamifyModel):
    listener_id: str
    kind: WebhookMessageKind
    event_id: str
    notam: NotamDTO
    sent_at: datetime
    change: WebhookLifecycleChange | None = None
    context: WebhookContext | None = None

    @model_validator(mode="after")
    def validate_change_payload(self) -> "WatcherWebhookEvent":
        if self.kind == WebhookMessageKind.lifecycle and self.change is None:
            raise ValueError("lifecycle webhook payloads must include change")
        if self.kind == WebhookMessageKind.interpretation and self.change is not None:
            raise ValueError("interpretation webhook payloads must not include change")
        return self


# Query DTOs
class ActiveNotamsQuery(NotamifyModel):
    excluded_classifications: list[str] | None = None
    notam_ids: list[str] | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    always_include_est: bool | None = None
    page: int | None = None
    per_page: int | None = None
    location: list[str] | None = None
    qcode: list[str] | None = None
    category: list[str] | None = None
    subcategory: list[str] | None = None
    affected_element: list[str] | None = None


class NearbyNotamsQuery(NotamifyModel):
    lat: float
    lon: float
    radius_nm: float | None = None
    page: int | None = None
    per_page: int | None = None
    excluded_classifications: list[str] | None = None
    qcode: list[str] | None = None
    notam_ids: list[str] | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    always_include_est: bool | None = None
    category: list[str] | None = None
    subcategory: list[str] | None = None
    affected_element: list[str] | None = None


class HistoricalNotamsQuery(NotamifyModel):
    valid_at: date
    notam_ids: list[str] | None = None
    always_include_est: bool | None = None
    page: int | None = None
    per_page: int | None = None
    location: list[str] | None = None
    category: list[str] | None = None
    subcategory: list[str] | None = None
    affected_element: list[str] | None = None


class ErrorResponse(NotamifyModel):
    error: str = ""
