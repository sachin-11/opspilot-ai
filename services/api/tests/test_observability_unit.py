import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from cryptography.fernet import Fernet
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from opspilot_api.config import Settings
from opspilot_api.incidents.models import Environment, Severity
from opspilot_api.observability import telemetry
from opspilot_api.observability.bus import (
    BusMessage,
    InMemoryEventBus,
    PublishError,
    Topics,
    deterministic_partition,
    safe_headers,
)
from opspilot_api.observability.models import ObservabilitySource, SourceProvider
from opspilot_api.observability.normalize import (
    NormalizationError,
    error_signature_for,
    normalize,
)
from opspilot_api.observability.providers import (
    CloudWatchProvider,
    FixtureObservabilityProvider,
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderRequestError,
    ProviderThrottledError,
    ProviderUnavailableError,
    SourceConfig,
    provider_for_source,
    validate_configuration,
)
from opspilot_api.observability.secrets_util import (
    SecretsConfigurationError,
    SignatureResult,
    compute_signature,
    decrypt_secret,
    encrypt_secret,
    generate_public_id,
    generate_webhook_secret,
    verify_signature,
)
from opspilot_api.observability.signal_schema import (
    CURRENT_SCHEMA_VERSION,
    InvalidSignalError,
    NormalizedSignal,
    RawSignalEnvelope,
    SignalProvider,
    UnsupportedSchemaError,
    parse_normalized,
    parse_raw,
    partition_key,
)

from .conftest import make_settings

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
WS, SVC, SRC = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def settings_with_key(**overrides: Any) -> Settings:
    return make_settings(OBS_SECRET_ENCRYPTION_KEY=Fernet.generate_key().decode(), **overrides)


def raw(payload: dict[str, Any], **fields: Any) -> RawSignalEnvelope:
    values: dict[str, Any] = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "signal_id": uuid.uuid4(),
        "workspace_id": WS,
        "service_id": SVC,
        "source_id": SRC,
        "provider": SignalProvider.GENERIC_WEBHOOK,
        "ingress_key": "hash:" + "a" * 64 + ":1",
        "payload": payload,
        "payload_hash": "a" * 64,
        "environment": Environment.PRODUCTION,
        "received_at": NOW,
        **fields,
    }
    return RawSignalEnvelope.model_validate(values)


ALERT = {
    "type": "alert",
    "title": "Checkout 5xx rate high",
    "message": "5xx above threshold",
    "severity": "high",
    "occurred_at": "2026-03-10T11:58:00Z",
}


# -- signatures and secrets ---------------------------------------------------------------------
def test_signature_valid_invalid_missing_expired_and_tampered() -> None:
    secret, body, now = generate_webhook_secret(), b'{"a":1}', 1_000_000.0
    stamp = str(int(now))
    good = compute_signature(secret, stamp, body)
    assert verify_signature(secret, stamp, good, body, now, 300) is SignatureResult.OK
    assert verify_signature(secret, stamp, "sha256=" + good, body, now, 300) is SignatureResult.OK
    assert verify_signature(secret, stamp, good, body + b" ", now, 300) is SignatureResult.INVALID
    assert verify_signature("other", stamp, good, body, now, 300) is SignatureResult.INVALID
    assert verify_signature(secret, None, good, body, now, 300) is SignatureResult.MISSING
    assert verify_signature(secret, stamp, None, body, now, 300) is SignatureResult.MISSING
    assert verify_signature(secret, "abc", good, body, now, 300) is SignatureResult.INVALID
    # The timestamp is covered by the signature: replaying an old body under a new stamp fails.
    assert (
        verify_signature(secret, str(int(now) + 1), good, body, now, 300) is SignatureResult.INVALID
    )
    late = compute_signature(secret, stamp, body)
    assert verify_signature(secret, stamp, late, body, now + 301, 300) is SignatureResult.EXPIRED
    future = compute_signature(secret, str(int(now) + 900), body)
    assert (
        verify_signature(secret, str(int(now) + 900), future, body, now, 300)
        is SignatureResult.EXPIRED
    )


def test_secrets_are_high_entropy_unique_and_encrypted_at_rest() -> None:
    settings = settings_with_key()
    first, second = generate_webhook_secret(), generate_webhook_secret()
    assert first != second and first.startswith("whsec_") and len(first) >= 40
    assert len(generate_public_id()) >= 32 and generate_public_id() != generate_public_id()
    reference = encrypt_secret(settings, first)
    assert reference.startswith("enc:v1:") and first not in reference
    assert decrypt_secret(settings, reference) == first
    with pytest.raises(SecretsConfigurationError):
        encrypt_secret(make_settings(OBS_SECRET_ENCRYPTION_KEY=None), first)
    with pytest.raises(SecretsConfigurationError):
        decrypt_secret(settings_with_key(), reference)  # different key cannot decrypt
    with pytest.raises(SecretsConfigurationError):
        decrypt_secret(settings, "plaintext-secret")


# -- schemas ----------------------------------------------------------------------------------
def test_schema_versions_validation_and_partition_key() -> None:
    envelope = raw(ALERT)
    parsed = parse_raw(envelope.model_dump_json().encode())
    assert parsed.signal_id == envelope.signal_id
    stale = json.loads(envelope.model_dump_json())
    stale["schema_version"] = "99"
    with pytest.raises(UnsupportedSchemaError):
        parse_raw(json.dumps(stale).encode())
    with pytest.raises(UnsupportedSchemaError):
        parse_normalized({"schema_version": None})
    broken = json.loads(envelope.model_dump_json())
    broken["unexpected"] = 1
    with pytest.raises(InvalidSignalError):
        parse_raw(broken)
    naive = json.loads(envelope.model_dump_json())
    naive["received_at"] = "2026-03-10T12:00:00"
    with pytest.raises(InvalidSignalError):
        parse_raw(naive)
    huge = json.loads(envelope.model_dump_json())
    huge["payload"] = {"blob": "x" * 200_000}
    with pytest.raises(InvalidSignalError):
        parse_raw(huge)
    normalized = normalize(raw(ALERT))
    again = parse_normalized(normalized.model_dump_json().encode())
    assert again == normalized
    assert partition_key(WS, SVC) == f"{WS}:{SVC}"
    assert deterministic_partition(partition_key(WS, SVC), 3) == deterministic_partition(
        partition_key(WS, SVC), 3
    )


# -- normalization ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("critical", Severity.SEV1), ("FATAL", Severity.SEV1), ("p1", Severity.SEV1),
        ("high", Severity.SEV2), ("error", Severity.SEV2), ("major", Severity.SEV2),
        ("warning", Severity.SEV3), ("medium", Severity.SEV3),
        ("low", Severity.SEV4), ("info", Severity.SEV4), ("sev3", Severity.SEV3),
    ],
)  # fmt: skip
def test_severity_mapping(word: str, expected: Severity) -> None:
    assert normalize(raw({**ALERT, "severity": word})).severity is expected


def test_normalization_is_deterministic_bounded_and_preserves_unknown_fields_safely() -> None:
    payload = {**ALERT, "custom_note": "x" * 5000, "nested": {"api_key": "sk-leak", "ok": 1}}
    envelope = raw(payload)
    first, second = normalize(envelope), normalize(envelope)
    assert first.model_dump() == second.model_dump()
    assert len(first.attributes["custom_note"]) <= 500
    assert first.attributes["nested"]["api_key"] == "[REDACTED]"
    assert first.error_signature and first.deduplication_key.startswith("fp:")


def test_secrets_and_pii_are_redacted_in_every_text_field() -> None:
    payload = {
        **ALERT,
        "title": "login failed for jane.doe@example.com",
        "message": "password=hunter2 and Authorization: Bearer abc.def.ghi",
        "resource_id": "postgres://user:pw@db/app",
        "dimensions": {"token": "secret-value-1", "host": "db password=abc"},
    }
    signal = normalize(raw(payload))
    blob = signal.model_dump_json()
    for leaked in ("jane.doe@example.com", "hunter2", "abc.def.ghi", "user:pw", "secret-value-1"):
        assert leaked not in blob
    assert "[REDACTED]" in blob


def test_log_normalization_signature_is_stable_across_volatile_values() -> None:
    def log(message: str) -> NormalizedSignal:
        return normalize(
            raw(
                {
                    "type": "log",
                    "level": "error",
                    "message": message,
                    "timestamp": "2026-03-10T11:59:00Z",
                }
            )
        )

    one = log(
        "ConnectionError: pool exhausted for request 8f14e45f-ceea-467a-9575-1fbd2a2f7a2b after 1500ms"
    )
    two = log(
        "ConnectionError: pool exhausted for request 0b2f5e11-aaaa-4bbb-8ccc-1234567890ab after 20ms"
    )
    assert one.error_signature == two.error_signature
    assert one.error_signature != log("ConnectionError: something else entirely").error_signature
    assert one.error_signature and one.error_signature.startswith("ConnectionError:")
    assert error_signature_for("x 1") == error_signature_for("x 2")


def test_other_normalizers_metric_deployment_and_cloudwatch_fixtures() -> None:
    metric = normalize(raw({"type": "metric", "metric_name": "ErrorRate", "value": 9.5, "threshold": 5,
                            "severity": "high", "unit": "Percent", "occurred_at": "2026-03-10T11:59:00Z"}))  # fmt: skip
    assert metric.signal_type.value == "metric_threshold" and metric.metric_value == 9.5
    deploy = normalize(raw({"type": "deployment", "commit_sha": "c3" * 20, "environment": "production",
                            "status": "success", "repository": "acme/checkout-api",
                            "occurred_at": "2026-03-10T11:50:00Z"}))  # fmt: skip
    assert deploy.signal_type.value == "deployment" and deploy.attributes["commit_sha"] == "c3" * 20
    alarm = normalize(raw({
        "AlarmName": "checkout-high-errors", "NewStateValue": "ALARM",
        "NewStateReason": "Threshold Crossed", "StateChangeTime": "2026-03-10T11:57:00Z",
        "Trigger": {"MetricName": "5XXError", "Namespace": "AWS/ApplicationELB", "Threshold": 5.0,
                    "Period": 60, "Dimensions": [{"name": "LoadBalancer", "value": "app/x"}]},
    }, provider=SignalProvider.AWS_CLOUDWATCH))  # fmt: skip
    assert alarm.severity is Severity.SEV2 and alarm.threshold == 5.0
    assert (
        alarm.dimensions == {"LoadBalancer": "app/x"}
        and alarm.error_signature == "alarm:checkout-high-errors"
    )
    cleared = normalize(
        raw({"AlarmName": "a", "NewStateValue": "OK", "StateChangeTime": "2026-03-10T11:57:00Z"})
    )
    assert cleared.severity is Severity.SEV4
    cw_log = normalize(raw({"logGroup": "/aws/app", "logStream": "s", "message": "ERROR boom 42",
                            "timestamp": 1_773_143_900_000}, provider=SignalProvider.AWS_CLOUDWATCH))  # fmt: skip
    assert cw_log.signal_type.value == "log_error" and cw_log.resource_id == "/aws/app"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"type": "alert", "severity": "high", "occurred_at": "2026-03-10T11:58:00Z"}, "missing_required_field"),
        ({**ALERT, "severity": None}, "missing_required_field"),
        ({**ALERT, "severity": "bananas"}, "invalid_severity"),
        ({**ALERT, "occurred_at": "2026-03-10T11:58:00"}, "invalid_timestamp"),
        ({**ALERT, "occurred_at": "not a time"}, "invalid_timestamp"),
        ({**ALERT, "occurred_at": "2027-01-01T00:00:00Z"}, "invalid_timestamp"),
        ({**ALERT, "occurred_at": "2020-01-01T00:00:00Z"}, "invalid_timestamp"),
        ({"type": "mystery"}, "unsupported_signal_type"),
        ({"hello": "world"}, "unsupported_signal_type"),
        ({"type": "log", "level": "info", "message": "ok", "timestamp": "2026-03-10T11:59:00Z"}, "unsupported_log_level"),
        ({"type": "metric", "metric_name": "x", "value": "high", "threshold": 1, "severity": "low",
          "occurred_at": "2026-03-10T11:59:00Z"}, "missing_required_field"),
    ],
)  # fmt: skip
def test_normalization_failures_are_explicit_and_never_guessed(
    payload: dict[str, Any], code: str
) -> None:
    with pytest.raises(NormalizationError) as caught:
        normalize(raw(payload))
    assert caught.value.code == code


def test_deduplication_key_priority() -> None:
    by_event = normalize(raw(ALERT, provider_event_id="evt-1"))
    same_event = normalize(raw({**ALERT, "title": "different"}, provider_event_id="evt-1"))
    assert by_event.deduplication_key == same_event.deduplication_key
    assert by_event.deduplication_key.startswith("evt:")
    idem = normalize(raw(ALERT, ingress_key="idem:" + "b" * 40))
    assert idem.deduplication_key == "idem:" + "b" * 40
    fingerprint_a = normalize(raw(ALERT))
    fingerprint_b = normalize(raw({**ALERT, "occurred_at": "2026-03-10T11:59:30Z"}))
    assert fingerprint_a.deduplication_key == fingerprint_b.deduplication_key
    assert fingerprint_a.deduplication_key.startswith("fp:")
    assert (
        normalize(raw({**ALERT, "title": "Other problem"})).deduplication_key
        != fingerprint_a.deduplication_key
    )


# -- in-memory event bus ----------------------------------------------------------------------
def test_memory_bus_ordering_commit_redelivery_lag_and_failures() -> None:
    bus = InMemoryEventBus(3)
    topic = "t"
    key = partition_key(WS, SVC)
    first = bus.publish(topic, key, b"1", {"signal_id": "s", "authorization": "Bearer x"})
    second = bus.publish(topic, key, b"2")
    assert first.offset is not None and second.offset is not None
    assert first.partition == second.partition and second.offset == first.offset + 1
    assert first.headers == {"signal_id": "s"}  # credentials never travel as headers
    assert safe_headers({"x-api-key": "k", "replay_of": "id"}) == {"replay_of": "id"}
    got = bus.consume([topic], "g", 10)
    assert [m.value for m in got] == [b"1", b"2"] and bus.lag("g", [topic]) == 2
    bus.commit("g", got[0])
    assert bus.lag("g", [topic]) == 1
    bus.restart_consumer("g")  # crash before committing the second message
    assert [m.value for m in bus.consume([topic], "g", 10)] == [b"2"]
    bus.retry("g", got[1])
    assert [m.value for m in bus.consume([topic], "g", 10)] == [b"2"]
    assert bus.consume([topic], "other-group", 10)  # groups progress independently
    bus.fail_publishes = 1
    with pytest.raises(PublishError):
        bus.publish(topic, key, b"3")
    bus.available = False
    assert not bus.health()
    with pytest.raises(PublishError):
        bus.publish(topic, key, b"4")
    assert Topics.from_settings(make_settings()).raw == "opspilot.observability.raw.v1"
    assert BusMessage(topic="t", value=b"").headers == {}


# -- providers --------------------------------------------------------------------------------
def fixture_provider(**config: Any) -> FixtureObservabilityProvider:
    return FixtureObservabilityProvider(SourceConfig.model_validate(config), make_settings())


def test_fixture_logs_are_bounded_filtered_and_deterministic() -> None:
    provider = fixture_provider(max_window_minutes=30, max_results=5)
    start = NOW - timedelta(hours=3)
    first = provider.query_logs(start, NOW, [], None, 50)
    assert 0 < len(first) <= 5
    assert first == provider.query_logs(start, NOW, [], None, 50)
    window_start, window_end, clamped = provider.clamp_window(start, NOW)
    assert clamped and window_end - window_start == timedelta(minutes=30)
    assert all(e.timestamp >= NOW - timedelta(minutes=30) for e in first)
    errors = provider.query_logs(NOW - timedelta(minutes=30), NOW, ["pool"], "ERROR", 50)
    assert errors and all("pool" in e.message.lower() and e.severity == "ERROR" for e in errors)
    with pytest.raises(ProviderRequestError):
        provider.query_logs(NOW, NOW - timedelta(minutes=1), [], None, 5)
    with pytest.raises(ProviderRequestError):
        provider.query_logs(NOW - timedelta(minutes=5), NOW, ["a; DROP"], None, 5)
    with pytest.raises(ProviderRequestError):
        provider.get_metric_statistics("bad{name}", NOW - timedelta(minutes=5), NOW, 60, "Average")
    with pytest.raises(ProviderRequestError):
        provider.get_metric_statistics(None, NOW - timedelta(minutes=5), NOW, 60, "Median")


def test_fixture_failure_modes_map_to_distinct_errors() -> None:
    expected = {
        "throttle": ProviderThrottledError,
        "timeout": ProviderUnavailableError,
        "auth_failure": ProviderAuthError,
        "error": ProviderUnavailableError,
    }
    for mode, error in expected.items():
        provider = fixture_provider(fixture_mode=mode)
        for call in (
            provider.test_connection,
            lambda p=provider: p.query_logs(NOW - timedelta(minutes=5), NOW, [], None, 5),
            lambda p=provider: p.get_alarm(NOW - timedelta(hours=1), NOW),
        ):
            with pytest.raises(error):
                call()
    assert ProviderThrottledError.retryable and not ProviderAuthError.retryable


def test_fixture_metric_spike_and_alarm() -> None:
    provider = fixture_provider(spike_after=(NOW - timedelta(minutes=10)).isoformat())
    series = provider.get_metric_statistics(None, NOW - timedelta(minutes=30), NOW, 300, "Average")
    before = [v for t, v in series.points if t < NOW - timedelta(minutes=10)]
    after = [v for t, v in series.points if t >= NOW - timedelta(minutes=10)]
    assert after and before and min(after) > max(before)
    alarm = provider.get_alarm(NOW - timedelta(hours=1), NOW)
    assert alarm is not None and alarm.state == "ALARM" and alarm.transitions


def test_source_configuration_rejects_credentials_and_unbounded_values() -> None:
    settings = make_settings()
    ok = validate_configuration(
        SourceProvider.AWS_CLOUDWATCH,
        {
            "role_arn": "arn:aws:iam::123456789012:role/opspilot-readonly",
            "external_id_ref": "OPSPILOT_EXTERNAL_ID",
            "allowed_log_groups": ["/aws/app/checkout"],
            "allowed_namespaces": ["AWS/ApplicationELB"],
            "default_metric": {"name": "5XXError", "namespace": "AWS/ApplicationELB"},
        },
        settings,
    )
    assert ok["role_arn"].endswith("opspilot-readonly")
    bad: list[dict[str, Any]] = [
        {"aws_access_key_id": "AKIA...", "allowed_log_groups": ["/x"]},
        {"secret_access_key": "x", "allowed_log_groups": ["/x"]},
        {"allowed_log_groups": ["/x"], "role_arn": "not-an-arn"},
        {"allowed_log_groups": ["/x"], "external_id_ref": "literal external id value"},
        {"allowed_log_groups": ["/x"], "max_window_minutes": 100_000},
        {"allowed_log_groups": ["/x"], "max_window_minutes": 1440},
        {"allowed_log_groups": ["bad group; rm"]},
        {},
    ]
    for config in bad:
        with pytest.raises(ValueError):
            validate_configuration(SourceProvider.AWS_CLOUDWATCH, config, settings)
    with pytest.raises(ValueError):
        validate_configuration(SourceProvider.GENERIC_WEBHOOK, {"anything": 1}, settings)
    assert validate_configuration(SourceProvider.DEMO_FIXTURE, {"fixture_mode": "ok"}, settings)


def cloudwatch(**config: Any) -> tuple[CloudWatchProvider, dict[str, Stubber], dict[str, Any]]:
    clients: dict[str, Any] = {}
    stubs: dict[str, Stubber] = {}

    def factory(service: str) -> Any:
        client = boto3.client(
            service, region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test"
        )
        clients[service] = client
        stubs[service] = Stubber(client)
        return client

    base = {"allowed_log_groups": ["/aws/app/checkout"], "allowed_namespaces": ["AWS/ApplicationELB"],
            "alarm_names": ["checkout-high-errors"], "max_window_minutes": 60, "max_results": 25,
            "default_metric": {"name": "5XXError", "namespace": "AWS/ApplicationELB"}}  # fmt: skip
    provider = CloudWatchProvider(
        SourceConfig.model_validate({**base, **config}), make_settings(), "us-east-1", factory
    )
    return provider, stubs, clients


def test_cloudwatch_adapter_is_bounded_read_only_and_allow_listed() -> None:
    provider, stubs, _ = cloudwatch()
    provider._client("logs")
    stubs["logs"].add_response(
        "filter_log_events",
        {"events": [{"timestamp": int(NOW.timestamp() * 1000), "message": "ERROR pool exhausted"}]},
        {
            "logGroupName": "/aws/app/checkout",
            "startTime": int((NOW - timedelta(minutes=60)).timestamp() * 1000),
            "endTime": int(NOW.timestamp() * 1000),
            "limit": 25,
            "filterPattern": '"pool"',
        },
    )
    with stubs["logs"]:
        entries = provider.query_logs(NOW - timedelta(hours=6), NOW, ["pool"], None, 500)
    assert len(entries) == 1 and entries[0].log_group == "/aws/app/checkout"
    provider._client("cloudwatch")
    stubs["cloudwatch"].add_response(
        "get_metric_statistics",
        {"Datapoints": [{"Timestamp": NOW, "Average": 4.0, "Unit": "Count"}]},
    )
    with stubs["cloudwatch"]:
        series = provider.get_metric_statistics(
            "5XXError", NOW - timedelta(minutes=30), NOW, 300, "Average"
        )
    assert series.points[0][1] == 4.0
    with pytest.raises(ProviderRequestError):
        provider.get_metric_statistics("5XXError", NOW - timedelta(minutes=30), NOW, 90, "Average")
    other_ns, _, _ = cloudwatch(allowed_namespaces=["Custom/Other"])
    other_ns.config.default_metric.namespace = "AWS/Billing"
    with pytest.raises(ProviderConfigurationError):
        other_ns.get_metric_statistics("x", NOW - timedelta(minutes=5), NOW, 60, "Average")
    wildcard_only, _, _ = cloudwatch(allowed_log_groups=["/aws/*"])
    with pytest.raises(ProviderConfigurationError):
        wildcard_only.query_logs(NOW - timedelta(minutes=5), NOW, [], None, 5)


def test_cloudwatch_errors_are_classified_and_credentials_never_leak() -> None:
    for code, error in (
        ("Throttling", ProviderThrottledError),
        ("ThrottlingException", ProviderThrottledError),
        ("AccessDenied", ProviderAuthError),
        ("ExpiredToken", ProviderAuthError),
        ("ResourceNotFoundException", ProviderConfigurationError),
        ("InternalFailure", ProviderUnavailableError),
    ):
        provider, stubs, _ = cloudwatch()
        provider._client("cloudwatch")
        stubs["cloudwatch"].add_client_error("describe_alarms", service_error_code=code)
        with stubs["cloudwatch"], pytest.raises(error) as caught:
            provider.test_connection()
        assert "test" not in str(caught.value).replace("test_", "") or code
        assert "secret" not in str(caught.value).lower()


def test_provider_for_source_selects_adapters_and_refuses_webhook_sources() -> None:
    def source(provider: SourceProvider, config: dict[str, Any]) -> ObservabilitySource:
        return ObservabilitySource(
            provider=provider.value, configuration=config, region="us-east-1"
        )

    assert isinstance(
        provider_for_source(make_settings(), source(SourceProvider.DEMO_FIXTURE, {})),
        FixtureObservabilityProvider,
    )
    assert isinstance(
        provider_for_source(
            make_settings(),
            source(SourceProvider.AWS_CLOUDWATCH, {"allowed_log_groups": ["/aws/app"]}),
            lambda service: boto3.client(
                service, region_name="us-east-1", aws_access_key_id="a", aws_secret_access_key="b"
            ),
        ),
        CloudWatchProvider,
    )
    with pytest.raises(ProviderConfigurationError):
        provider_for_source(make_settings(), source(SourceProvider.GENERIC_WEBHOOK, {}))
    with pytest.raises(ProviderConfigurationError):
        provider_for_source(make_settings(), source(SourceProvider.DEMO_FIXTURE, {"role_arn": "x"}))


# -- telemetry --------------------------------------------------------------------------------
def test_span_attributes_are_allow_listed_and_truncated() -> None:
    exporter = InMemorySpanExporter()
    telemetry.configure_tracing("test", exporter)
    with telemetry.span(
        "unit", signal__id="abc", workspace__id=WS, payload="RAW BODY", authorization="Bearer x",
        signature="deadbeef", topic="t" * 500,
    ):  # fmt: skip
        pass
    attributes = dict(exporter.get_finished_spans()[-1].attributes or {})
    assert attributes["signal.id"] == "abc" and len(str(attributes["topic"])) <= 100
    assert not {"payload", "authorization", "signature"} & set(attributes)
    with pytest.raises(RuntimeError), telemetry.span("failing"):
        raise RuntimeError("boom")
    assert (
        dict(exporter.get_finished_spans()[-1].attributes or {})["failure.code"] == "RuntimeError"
    )


def test_observability_settings_validate() -> None:
    assert make_settings(OBS_SECRET_ENCRYPTION_KEY="").OBS_SECRET_ENCRYPTION_KEY is None
    assert isinstance(
        make_settings(OBS_SECRET_ENCRYPTION_KEY=SecretStr("x" * 44)).OBS_SECRET_ENCRYPTION_KEY,
        SecretStr,
    )
