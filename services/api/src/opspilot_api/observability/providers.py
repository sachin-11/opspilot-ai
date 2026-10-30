"""Read-only observability providers: deterministic fixture adapter and bounded CloudWatch adapter.

Scope comes only from the stored source configuration. Callers (including investigation tools)
cannot choose AWS services, accounts, role ARNs, log groups, or namespaces; they pass bounded
search parameters and the provider enforces allow-lists, windows, and result limits.
"""

import fnmatch
import os
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from opspilot_api.config import Settings
from opspilot_api.investigations.safety import redact_text
from opspilot_api.observability.models import ObservabilitySource, SourceProvider

SAFE_TERM = re.compile(r"^[A-Za-z0-9_.:/@=-]{1,64}$")
SAFE_METRIC = re.compile(r"^[A-Za-z0-9_.:/ -]{1,255}$")
STATISTICS = ("Average", "Sum", "Minimum", "Maximum", "SampleCount")
_ROLE_ARN = re.compile(r"^arn:aws:iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]{1,200}$")
_ENV_REF = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_CREDENTIAL_KEYS = ("access_key", "secret", "token", "password", "credential", "session")


class ObsProviderError(Exception):
    code = "provider_error"
    retryable = False


class ProviderThrottledError(ObsProviderError):
    code = "provider_throttled"
    retryable = True


class ProviderAuthError(ObsProviderError):
    code = "provider_authentication_failed"


class ProviderUnavailableError(ObsProviderError):
    code = "provider_unavailable"
    retryable = True


class ProviderConfigurationError(ObsProviderError):
    code = "provider_misconfigured"


class ProviderRequestError(ObsProviderError):
    code = "provider_invalid_request"


@dataclass(frozen=True)
class LogEntry:
    timestamp: datetime
    message: str
    severity: str | None
    log_group: str | None


@dataclass(frozen=True)
class MetricSeries:
    metric_name: str
    statistic: str
    unit: str | None
    period_seconds: int
    points: list[tuple[datetime, float]]


@dataclass(frozen=True)
class AlarmInfo:
    name: str
    state: str
    metric_name: str | None
    threshold: float | None
    comparison: str | None
    period_seconds: int | None
    transitions: Sequence[tuple[datetime, str | None, str, str | None]] = field(
        default_factory=list
    )


class DefaultMetric(BaseModel):
    name: str = Field(default="ErrorRate", max_length=255)
    namespace: str = Field(default="OpsPilot/Fixture", max_length=255)
    dimensions: dict[str, str] = Field(default_factory=dict)
    statistic: Literal["Average", "Sum", "Minimum", "Maximum", "SampleCount"] = "Average"

    @field_validator("name")
    @classmethod
    def _metric(cls, value: str) -> str:
        if not SAFE_METRIC.match(value):
            raise ValueError("metric name contains unsupported characters")
        return value


class SourceConfig(BaseModel):
    """Validated, credential-free configuration shared by CloudWatch and fixture sources."""

    role_arn: str | None = None
    external_id_ref: str | None = None
    allowed_log_groups: list[str] = Field(default_factory=list, max_length=10)
    allowed_namespaces: list[str] = Field(default_factory=list, max_length=10)
    alarm_names: list[str] = Field(default_factory=list, max_length=10)
    max_window_minutes: int = Field(default=60, ge=1, le=1440)
    max_results: int = Field(default=50, ge=1, le=1000)
    timeout_seconds: float = Field(default=10, gt=0, le=60)
    default_metric: DefaultMetric = Field(default_factory=DefaultMetric)
    fixture_mode: Literal["ok", "throttle", "timeout", "auth_failure", "error"] = "ok"
    spike_after: datetime | None = None

    @field_validator("role_arn")
    @classmethod
    def _role(cls, value: str | None) -> str | None:
        if value is not None and not _ROLE_ARN.match(value):
            raise ValueError("role_arn must be an IAM role ARN")
        return value

    @field_validator("external_id_ref")
    @classmethod
    def _external(cls, value: str | None) -> str | None:
        if value is not None and not _ENV_REF.match(value):
            raise ValueError("external_id_ref must name an environment variable, not a value")
        return value

    @field_validator("allowed_log_groups", "allowed_namespaces", "alarm_names")
    @classmethod
    def _patterns(cls, value: list[str]) -> list[str]:
        for item in value:
            if not re.match(r"^[A-Za-z0-9_./*:#-]{1,200}$", item):
                raise ValueError("patterns may only contain safe characters")
        return value


def validate_configuration(
    provider: SourceProvider, raw: dict[str, Any], settings: Settings
) -> dict[str, Any]:
    """Return sanitized configuration or raise ``ValueError``; rejects credential-looking keys."""
    for key in raw:
        if any(marker in key.lower() for marker in _CREDENTIAL_KEYS) and key != "external_id_ref":
            raise ValueError(
                f"configuration key {key!r} looks like a credential and is not allowed"
            )
    if provider is SourceProvider.GENERIC_WEBHOOK:
        if raw:
            raise ValueError("generic webhook sources take no provider configuration")
        return {}
    config = SourceConfig.model_validate(raw)
    if config.max_window_minutes > settings.OBS_CLOUDWATCH_MAX_WINDOW_MINUTES:
        raise ValueError("max_window_minutes exceeds the deployment limit")
    if config.max_results > settings.OBS_CLOUDWATCH_MAX_RESULTS:
        raise ValueError("max_results exceeds the deployment limit")
    if (
        provider is SourceProvider.AWS_CLOUDWATCH
        and not config.allowed_log_groups
        and not config.allowed_namespaces
    ):
        raise ValueError("allowed_log_groups or allowed_namespaces is required")
    if (
        provider is SourceProvider.AWS_CLOUDWATCH
        and config.allowed_namespaces
        and not any(
            fnmatch.fnmatch(config.default_metric.namespace, p) for p in config.allowed_namespaces
        )
    ):
        raise ValueError("default_metric.namespace must match allowed_namespaces")
    return config.model_dump(mode="json", exclude_none=True)


class ObservabilityProvider(ABC):
    name: str

    def __init__(self, config: SourceConfig, settings: Settings) -> None:
        self.config = config
        self.settings = settings
        self.max_window = timedelta(
            minutes=min(config.max_window_minutes, settings.OBS_CLOUDWATCH_MAX_WINDOW_MINUTES)
        )
        self.max_results = min(config.max_results, settings.OBS_CLOUDWATCH_MAX_RESULTS)

    def clamp_window(self, start: datetime, end: datetime) -> tuple[datetime, datetime, bool]:
        """Reject invalid ranges; clamp oversized windows to their newest part."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ProviderRequestError("time window must include a timezone")
        if end <= start:
            raise ProviderRequestError("end must be after start")
        if end - start > self.max_window:
            return end - self.max_window, end, True
        return start, end, False

    @abstractmethod
    def test_connection(self) -> None: ...

    @abstractmethod
    def query_logs(
        self, start: datetime, end: datetime, terms: list[str], severity: str | None, limit: int
    ) -> list[LogEntry]: ...

    @abstractmethod
    def get_metric_statistics(
        self, metric_name: str | None, start: datetime, end: datetime, period: int, statistic: str
    ) -> MetricSeries: ...

    @abstractmethod
    def get_alarm(self, start: datetime, end: datetime) -> AlarmInfo | None: ...

    def validate_terms(self, terms: list[str]) -> list[str]:
        cleaned = [term for term in terms if term]
        for term in cleaned:
            if not SAFE_TERM.match(term):
                raise ProviderRequestError("search terms may only contain safe characters")
        return cleaned[:5]


class FixtureObservabilityProvider(ObservabilityProvider):
    """Deterministic logs/metrics/alarms plus injectable throttling, timeout, and failures."""

    name = "fixture"

    def _fail_if_configured(self) -> None:
        mode = self.config.fixture_mode
        if mode == "throttle":
            raise ProviderThrottledError("fixture provider is throttling requests")
        if mode == "timeout":
            raise ProviderUnavailableError("fixture provider request timed out")
        if mode == "auth_failure":
            raise ProviderAuthError("fixture provider rejected the credentials")
        if mode == "error":
            raise ProviderUnavailableError("fixture provider is unavailable")

    def test_connection(self) -> None:
        self._fail_if_configured()

    _LINES = (
        ("ERROR", "database connection pool exhausted (waiting={n}) request_id={rid}"),
        ("ERROR", "timeout acquiring database connection after 1500ms request_id={rid}"),
        ("WARN", "slow query detected duration_ms={n} request_id={rid}"),
        ("ERROR", "payment gateway timeout for order {n} api_key=sk-fixture{n}secret"),
        ("INFO", "request completed status=200 duration_ms={n}"),
    )

    def query_logs(
        self, start: datetime, end: datetime, terms: list[str], severity: str | None, limit: int
    ) -> list[LogEntry]:
        self._fail_if_configured()
        start, end, _ = self.clamp_window(start, end)
        terms = self.validate_terms(terms)
        limit = min(limit, self.max_results)
        group = (self.config.allowed_log_groups or ["/fixture/service"])[0].replace("*", "app")
        entries: list[LogEntry] = []
        minute = start.replace(second=0, microsecond=0)
        index = 0
        while minute < end and len(entries) < 1000:
            level, template = self._LINES[(int(minute.timestamp()) // 60) % len(self._LINES)]
            number = 100 + index % 37
            message = template.format(n=number, rid=f"req-{int(minute.timestamp()) % 100000}")
            if minute >= start:
                entries.append(LogEntry(minute, message, level, group))
            minute += timedelta(minutes=1)
            index += 1
        matched = [
            entry
            for entry in entries
            if (not terms or any(t.lower() in entry.message.lower() for t in terms))
            and (severity is None or (entry.severity or "").upper() == severity.upper())
        ]
        return matched[:limit]

    def get_metric_statistics(
        self, metric_name: str | None, start: datetime, end: datetime, period: int, statistic: str
    ) -> MetricSeries:
        self._fail_if_configured()
        start, end, _ = self.clamp_window(start, end)
        name = metric_name or self.config.default_metric.name
        if not SAFE_METRIC.match(name):
            raise ProviderRequestError("metric name contains unsupported characters")
        if statistic not in STATISTICS:
            raise ProviderRequestError("unsupported statistic")
        points: list[tuple[datetime, float]] = []
        cursor = start
        spike = self.config.spike_after
        while cursor < end and len(points) < 500:
            base = 100.0 + (int(cursor.timestamp()) // period) % 5
            value = base * 4 if spike is not None and cursor >= spike else base
            points.append((cursor, float(value)))
            cursor += timedelta(seconds=period)
        return MetricSeries(name, statistic, "Count", period, points)

    def get_alarm(self, start: datetime, end: datetime) -> AlarmInfo | None:
        self._fail_if_configured()
        name = (self.config.alarm_names or ["fixture-high-error-rate"])[0]
        return AlarmInfo(
            name=name,
            state="ALARM",
            metric_name=self.config.default_metric.name,
            threshold=5.0,
            comparison="GreaterThanThreshold",
            period_seconds=60,
            transitions=[
                (end - timedelta(minutes=5), "OK", "ALARM", "Threshold Crossed: 3 datapoints"),
                (start, None, "OK", "Alarm created"),
            ],
        )


class CloudWatchProvider(ObservabilityProvider):
    """Bounded read-only CloudWatch Logs/Metrics adapter using temporary role credentials.

    Only the allow-listed operations below are ever invoked: ``sts:AssumeRole`` (when a role is
    configured), ``logs:FilterLogEvents``, ``cloudwatch:GetMetricStatistics``,
    ``cloudwatch:DescribeAlarms``, and ``cloudwatch:DescribeAlarmHistory``. No static keys are
    stored; credentials come from the assumed role or the default credential chain.
    """

    name = "aws_cloudwatch"

    def __init__(
        self,
        config: SourceConfig,
        settings: Settings,
        region: str | None,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        super().__init__(config, settings)
        self.region = region
        self._client_factory = client_factory or self._default_client
        self._credentials: dict[str, Any] | None = None
        self._clients: dict[str, Any] = {}

    def _boto_config(self) -> Any:
        from botocore.config import Config

        timeout = min(self.config.timeout_seconds, self.settings.OBS_CLOUDWATCH_TIMEOUT_SECONDS)
        return Config(
            connect_timeout=timeout,
            read_timeout=timeout,
            retries={"max_attempts": 1},
            region_name=self.region,
        )

    def _assumed(self) -> dict[str, Any]:
        import boto3

        if self._credentials and self._credentials["Expiration"] > datetime.now(UTC) + timedelta(
            minutes=2
        ):
            return self._credentials
        args: dict[str, Any] = {
            "RoleArn": self.config.role_arn,
            "RoleSessionName": "opspilot-readonly",
            "DurationSeconds": 900,
        }
        if self.config.external_id_ref:
            external = os.environ.get(self.config.external_id_ref)
            if not external:
                raise ProviderConfigurationError(
                    "external ID reference is not set in the environment"
                )
            args["ExternalId"] = external
        sts = boto3.client("sts", config=self._boto_config())
        self._credentials = self._map_errors(lambda: sts.assume_role(**args))["Credentials"]
        return self._credentials or {}

    def _default_client(self, service: str) -> Any:
        import boto3

        if self.config.role_arn:
            credentials = self._assumed()
            return boto3.client(
                service,
                aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"],
                aws_session_token=credentials["SessionToken"],
                config=self._boto_config(),
            )
        return boto3.client(service, config=self._boto_config())

    def _client(self, service: str) -> Any:
        client = self._clients.get(service)
        if client is None:
            client = self._client_factory(service)
            self._clients[service] = client
        return client

    @staticmethod
    def _map_errors(call: Callable[[], Any]) -> Any:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return call()
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"Throttling", "ThrottlingException", "RequestLimitExceeded",
                        "TooManyRequestsException", "LimitExceededException"}:  # fmt: skip
                raise ProviderThrottledError("CloudWatch is throttling requests") from exc
            if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
                        "ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId",
                        "UnrecognizedClientException", "SignatureDoesNotMatch"}:  # fmt: skip
                raise ProviderAuthError("AWS rejected the credentials or role permissions") from exc
            if code in {"ResourceNotFoundException", "InvalidParameterValueException"}:
                raise ProviderConfigurationError(
                    "configured CloudWatch resource was not found"
                ) from exc
            raise ProviderUnavailableError(
                f"CloudWatch request failed ({code or 'error'})"
            ) from exc
        except BotoCoreError as exc:
            raise ProviderUnavailableError(
                f"CloudWatch request failed: {type(exc).__name__}"
            ) from exc

    def test_connection(self) -> None:
        self._map_errors(lambda: self._client("cloudwatch").describe_alarms(MaxRecords=1))

    def _allowed_groups(self) -> list[str]:
        groups = [g for g in self.config.allowed_log_groups if "*" not in g]
        if not groups:
            raise ProviderConfigurationError("no concrete allowed log group is configured")
        for group in groups:
            if not any(
                fnmatch.fnmatch(group, pattern) for pattern in self.config.allowed_log_groups
            ):
                raise ProviderConfigurationError("log group is not allowed by the source")
        return groups[:3]

    def query_logs(
        self, start: datetime, end: datetime, terms: list[str], severity: str | None, limit: int
    ) -> list[LogEntry]:
        start, end, _ = self.clamp_window(start, end)
        terms = self.validate_terms(terms)
        limit = min(limit, self.max_results)
        pattern = " ".join(f'"{term}"' for term in terms)
        if severity:
            if not re.match(r"^[A-Za-z]{3,10}$", severity):
                raise ProviderRequestError("invalid severity filter")
            pattern = f'{pattern} "{severity.upper()}"'.strip()
        entries: list[LogEntry] = []
        for group in self._allowed_groups():
            params: dict[str, Any] = {
                "logGroupName": group,
                "startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000),
                "limit": min(limit, 100),
            }
            if pattern:
                params["filterPattern"] = pattern
            response = self._filter_events(params)
            for event in response.get("events", []):
                message = str(event.get("message", ""))
                entries.append(
                    LogEntry(
                        datetime.fromtimestamp(event["timestamp"] / 1000, UTC),
                        message,
                        _severity_of(message),
                        group,
                    )
                )
        entries.sort(key=lambda e: e.timestamp)
        return entries[:limit]

    def _filter_events(self, params: dict[str, Any]) -> Any:
        return self._map_errors(lambda: self._client("logs").filter_log_events(**params))

    def _namespace(self, requested: str | None) -> str:
        namespace = requested or self.config.default_metric.namespace
        if not any(fnmatch.fnmatch(namespace, p) for p in self.config.allowed_namespaces):
            raise ProviderConfigurationError("metric namespace is not allowed by the source")
        return namespace

    def get_metric_statistics(
        self, metric_name: str | None, start: datetime, end: datetime, period: int, statistic: str
    ) -> MetricSeries:
        start, end, _ = self.clamp_window(start, end)
        name = metric_name or self.config.default_metric.name
        if not SAFE_METRIC.match(name):
            raise ProviderRequestError("metric name contains unsupported characters")
        if statistic not in STATISTICS:
            raise ProviderRequestError("unsupported statistic")
        if period < 60 or period % 60:
            raise ProviderRequestError("period must be a positive multiple of 60 seconds")
        response = self._map_errors(
            lambda: self._client("cloudwatch").get_metric_statistics(
                Namespace=self._namespace(None),
                MetricName=name,
                Dimensions=[
                    {"Name": k, "Value": v}
                    for k, v in self.config.default_metric.dimensions.items()
                ],
                StartTime=start,
                EndTime=end,
                Period=period,
                Statistics=[statistic],
            )
        )
        points = sorted(
            ((p["Timestamp"], float(p[statistic])) for p in response.get("Datapoints", [])),
            key=lambda item: item[0],
        )[: self.max_results]
        unit = (
            response.get("Datapoints", [{}])[0].get("Unit") if response.get("Datapoints") else None
        )
        return MetricSeries(name, statistic, unit, period, points)

    def get_alarm(self, start: datetime, end: datetime) -> AlarmInfo | None:
        start, end, _ = self.clamp_window(start, end)
        names = self.config.alarm_names
        if not names or any("*" in n for n in names):
            raise ProviderConfigurationError("explicit alarm names are required")
        described = self._map_errors(
            lambda: self._client("cloudwatch").describe_alarms(AlarmNames=names[:1], MaxRecords=1)
        )
        alarms = described.get("MetricAlarms", [])
        if not alarms:
            return None
        alarm = alarms[0]
        history = self._map_errors(
            lambda: self._client("cloudwatch").describe_alarm_history(
                AlarmName=alarm["AlarmName"],
                HistoryItemType="StateUpdate",
                StartDate=start,
                EndDate=end,
                MaxRecords=20,
            )
        )
        transitions = []
        for item in history.get("AlarmHistoryItems", [])[:20]:
            summary = str(item.get("HistorySummary", ""))
            transitions.append((item["Timestamp"], None, _state_from(summary), summary[:300]))
        return AlarmInfo(
            name=alarm["AlarmName"],
            state=alarm.get("StateValue", "INSUFFICIENT_DATA"),
            metric_name=alarm.get("MetricName"),
            threshold=alarm.get("Threshold"),
            comparison=alarm.get("ComparisonOperator"),
            period_seconds=alarm.get("Period"),
            transitions=transitions,
        )


def _severity_of(message: str) -> str | None:
    upper = message.upper()
    for level in ("FATAL", "CRITICAL", "ERROR", "WARN", "INFO"):
        if level in upper[:80]:
            return level
    return None


def _state_from(summary: str) -> str:
    for state in ("INSUFFICIENT_DATA", "ALARM", "OK"):
        if f"to {state}" in summary:
            return state
    return "UNKNOWN"


def provider_for_source(
    settings: Settings,
    source: ObservabilitySource,
    client_factory: Callable[[str], Any] | None = None,
) -> ObservabilityProvider:
    if source.provider == SourceProvider.GENERIC_WEBHOOK.value:
        raise ProviderConfigurationError("webhook sources have no query provider")
    try:
        config = SourceConfig.model_validate(source.configuration)
    except ValueError as exc:
        raise ProviderConfigurationError("stored source configuration is invalid") from exc
    if source.provider == SourceProvider.DEMO_FIXTURE.value:
        return FixtureObservabilityProvider(config, settings)
    return CloudWatchProvider(config, settings, source.region, client_factory)


def redact_log_message(message: str, limit: int = 500) -> str:
    return redact_text(message, max_length=limit)


def monotonic_ms() -> float:
    return time.perf_counter() * 1000
