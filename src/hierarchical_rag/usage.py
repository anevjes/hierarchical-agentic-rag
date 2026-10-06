import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from time import perf_counter
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class TokenRates(BaseModel):
    """USD per million tokens, supplied by the operator, never inferred."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    input: float = Field(ge=0)
    output: float = Field(default=0, ge=0)
    cached_input: float | None = Field(default=None, ge=0)


class UsageEvent(BaseModel):
    stage: str
    deployment: str | None = None
    document: str | None = None
    status: Literal["running", "completed", "failed"] = "running"
    elapsed_seconds: float = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    estimated_token_cost_usd: float | None = None

    def record(self, details: Mapping[str, object] | None) -> None:
        if details is None:
            return
        for source, target in (
            ("input_token_count", "input_tokens"),
            ("output_token_count", "output_tokens"),
            ("total_token_count", "total_tokens"),
            ("cache_read_input_token_count", "cached_input_tokens"),
            ("reasoning_output_token_count", "reasoning_output_tokens"),
        ):
            value = details.get(source)
            if value is not None:
                if type(value) is not int or value < 0:
                    raise ValueError(f"Invalid reported token counter: {source}")
                setattr(self, target, value)
        if (
            self.total_tokens is None
            and self.input_tokens is not None
            and self.output_tokens is not None
        ):
            self.total_tokens = self.input_tokens + self.output_tokens


class UsageReport(BaseModel):
    run_id: str
    command: str
    status: str
    started_at: datetime
    finished_at: datetime
    elapsed_seconds: float
    counters: dict[str, int]
    reported_tokens: dict[str, int | None]
    events: list[UsageEvent]
    rates_usd_per_million: dict[str, TokenRates]
    estimated_token_cost_usd: float | None
    unpriced_events: int
    limitations: list[str]


class UsageTracker:
    def __init__(self, command: str, rates: dict[str, TokenRates] | None = None) -> None:
        self.command = command
        self.rates = rates or {}
        self.run_id = uuid4().hex
        self.started_at = datetime.now(UTC)
        self.started = perf_counter()
        self.events: list[UsageEvent] = []
        self.counters: dict[str, int] = {}

    def increment(self, name: str, value: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + value

    @contextmanager
    def operation(
        self, stage: str, deployment: str | None = None, document: str | None = None
    ) -> Iterator[UsageEvent]:
        event = UsageEvent(stage=stage, deployment=deployment, document=document)
        self.events.append(event)
        started = perf_counter()
        completed = False
        try:
            yield event
            completed = True
        finally:
            event.status = "completed" if completed else "failed"
            event.elapsed_seconds = round(perf_counter() - started, 3)
            rate = self.rates.get(deployment) if deployment else None
            if (
                rate is not None
                and event.input_tokens is not None
                and event.output_tokens is not None
            ):
                cached = event.cached_input_tokens
                if rate.cached_input is None or cached is not None:
                    cached = cached or 0
                    if cached > event.input_tokens:
                        logger.warning("Cached token count exceeds input tokens; cost unavailable")
                    else:
                        event.estimated_token_cost_usd = (
                            (event.input_tokens - cached) * rate.input
                            + cached * (
                                rate.cached_input if rate.cached_input is not None else rate.input
                            )
                            + event.output_tokens * rate.output
                        ) / 1_000_000
            logger.info(
                "Usage stage=%s deployment=%s document=%s status=%s elapsed=%.3fs "
                "input_tokens=%s output_tokens=%s cached_input_tokens=%s "
                "reasoning_output_tokens=%s estimated_token_cost_usd=%s",
                stage, deployment, document, event.status, event.elapsed_seconds,
                event.input_tokens, event.output_tokens, event.cached_input_tokens,
                event.reasoning_output_tokens, event.estimated_token_cost_usd,
            )

    def report(self, status: str) -> UsageReport:
        tokens: dict[str, int | None] = {}
        for field in (
            "input_tokens", "output_tokens", "total_tokens",
            "cached_input_tokens", "reasoning_output_tokens",
        ):
            known = [
                getattr(event, field) for event in self.events if getattr(event, field) is not None
            ]
            tokens[field] = sum(known) if known else None
        costs = [
            event.estimated_token_cost_usd for event in self.events
            if event.estimated_token_cost_usd is not None
        ]
        return UsageReport(
            run_id=self.run_id,
            command=self.command,
            status=status,
            started_at=self.started_at,
            finished_at=datetime.now(UTC),
            elapsed_seconds=round(perf_counter() - self.started, 3),
            counters=dict(self.counters),
            reported_tokens=tokens,
            events=[event.model_copy(deep=True) for event in self.events],
            rates_usd_per_million=self.rates,
            estimated_token_cost_usd=sum(costs) if costs else None,
            unpriced_events=sum(event.estimated_token_cost_usd is None for event in self.events),
            limitations=[
                "Token totals sum reported counters only; null means unavailable, not zero.",
                "CU internal tokens and IQ/Search query-vectorization tokens are not exposed.",
                "MAF usage includes tool-loop calls; aborted runs may omit earlier calls.",
                "SDK retries, timeouts and missing usage can leave billed work unreported.",
                "Cost is a partial token-only USD estimate from operator rates, not an Azure bill.",
                "CU page meters, Search capacity/semantic, Blob, networking and taxes excluded.",
                "Cached/reasoning tokens are subsets of input/output, not additional tokens.",
                "Without a cached_input rate, all reported input tokens use the input rate.",
            ],
        )

    def log_summary(self, report: UsageReport) -> None:
        logger.info(
            "Usage summary run=%s status=%s elapsed=%.3fs counters=%s reported_tokens=%s "
            "partial_estimated_token_cost_usd=%s unpriced_events=%d",
            report.run_id, report.status, report.elapsed_seconds, report.counters,
            report.reported_tokens, report.estimated_token_cost_usd, report.unpriced_events,
        )
        logger.info("Cost coverage: %s", " ".join(report.limitations))
