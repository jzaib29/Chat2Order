"""Typed application records and compact schemas used by the three agents."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Product(Record):
    id: str
    name: str
    price: int = Field(ge=0, le=1_000_000)
    daily_capacity: int = Field(ge=0, le=10_000)
    aliases: list[str] = Field(default_factory=list)


class Business(Record):
    name: str = "Sunday & Sugar"
    currency: str = "PKR"
    timezone: str = "Asia/Karachi"
    lead_hours: int = Field(default=12, ge=0, le=720)
    pickup_start: str = "10:00"
    pickup_end: str = "18:00"
    products: list[Product]
    policy_text: str

    @field_validator("pickup_start", "pickup_end")
    @classmethod
    def valid_time(cls, value: str) -> str:
        datetime.strptime(value, "%H:%M")
        return value


class Message(Record):
    id: str
    conversation_id: str
    customer: str
    timestamp: str
    sender: Literal["customer", "seller"]
    text: str = Field(min_length=1, max_length=3000)


class OrderEvent(Record):
    """All fields are required (nullable where appropriate) for strict JSON mode."""
    conversation_id: str
    source_message_id: str
    action: Literal["create", "update", "cancel", "clarify", "ignore"]
    product_id: str | None
    quantity: int | None
    pickup_date: str | None
    pickup_time: str | None
    question: str | None
    explanation: str


class EventBatch(Record):
    events: list[OrderEvent]


class Issue(Record):
    code: str
    text: str


class Order(Record):
    id: str
    conversation_id: str
    customer: str
    product_id: str | None = None
    quantity: int | None = None
    pickup_date: str | None = None
    pickup_time: str | None = None
    cancelled: bool = False
    source_ids: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    status: str = "needs_clarification"
    issues: list[Issue] = Field(default_factory=list)


class Reservation(Record):
    order_id: str
    customer: str
    conversation_id: str
    product_id: str
    quantity: int = Field(ge=1, le=10_000)
    pickup_date: str
    pickup_time: str
    unit_price: int = Field(ge=0)
    approved_at: str
    version: int = Field(default=1, ge=1)


class Alternative(Record):
    id: str
    product_id: str
    product_name: str
    quantity: int
    pickup_date: str
    pickup_time: str
    unit_price: int
    available: int


class Resolution(Record):
    order_id: str
    explanation: str
    alternative_ids: list[str]
    questions: list[str]
    policy_ids: list[str]


class ResolutionBatch(Record):
    cases: list[Resolution]


class CustomerReply(Record):
    order_id: str
    text: str


class ReplyBatch(Record):
    messages: list[CustomerReply]


class Trace(Record):
    stage: str
    agent: str
    detail: str
    duration_ms: int = 0
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached: bool = False


class Snapshot(Record):
    messages: list[Message] = Field(default_factory=list)
    events: list[OrderEvent] = Field(default_factory=list)
    orders: list[Order] = Field(default_factory=list)
    resolutions: list[Resolution] = Field(default_factory=list)
    replies: dict[str, str] = Field(default_factory=dict)
    alternatives: dict[str, list[Alternative]] = Field(default_factory=dict)
    traces: list[Trace] = Field(default_factory=list)
    mode: Literal["demo", "live"] = "demo"
    elapsed_ms: int = 0
    processed_at: str = ""
    fingerprint: str = ""


class Workspace(Record):
    format_version: Literal[1] = 1
    business: Business
    inbox_text: str
    snapshot: Snapshot
    reservations: dict[str, Reservation] = Field(default_factory=dict)
    audit: list[dict[str, str]] = Field(default_factory=list)
