"""Broker-neutral event bus with a Kafka-compatible adapter and a deterministic in-memory adapter.

Domain code depends on ``EventBus``/``BusMessage`` only, never on a Kafka library message type.

Delivery semantics: ``publish`` returns only after the broker acknowledged the record (acks=all),
``consume`` never commits, and ``commit`` is explicit, so a consumer that crashes before commit
sees the message again (at-least-once). Ordering is guaranteed only per partition; the partition
key is ``workspace_id:service_id`` so one service's signals stay ordered while different services
may interleave.
"""

import logging
import threading
import time
import zlib
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from opspilot_api.config import Settings

logger = logging.getLogger(__name__)

# Headers are routing metadata only: never credentials, signatures, or payload contents.
SAFE_HEADERS = {
    "schema_version",
    "signal_id",
    "workspace_id",
    "source_id",
    "replay_of",
    "content_type",
}


class BusError(Exception):
    retryable = True


class PublishError(BusError):
    """The broker did not durably acknowledge the record."""


class BusUnavailableError(BusError):
    """The broker cannot currently be reached."""


@dataclass(frozen=True)
class BusMessage:
    topic: str
    value: bytes
    key: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    partition: int | None = None
    offset: int | None = None
    timestamp_ms: int | None = None


@dataclass(frozen=True)
class Topics:
    raw: str
    normalized: str
    dead_letter: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "Topics":
        prefix = settings.KAFKA_TOPIC_PREFIX
        return cls(f"{prefix}.raw.v1", f"{prefix}.normalized.v1", f"{prefix}.dead-letter.v1")

    def all(self) -> list[str]:
        return [self.raw, self.normalized, self.dead_letter]


def safe_headers(headers: dict[str, str] | None) -> dict[str, str]:
    return {k: v[:200] for k, v in (headers or {}).items() if k in SAFE_HEADERS}


class EventBus(ABC):
    backend: str

    @abstractmethod
    def ensure_topics(self, topics: list[str], partitions: int) -> None: ...

    @abstractmethod
    def publish(
        self, topic: str, key: str | None, value: bytes, headers: dict[str, str] | None = None
    ) -> BusMessage:
        """Block until durably acknowledged; raise ``PublishError`` otherwise."""

    @abstractmethod
    def consume(
        self, topics: list[str], group: str, max_messages: int = 1, timeout: float = 1.0
    ) -> list[BusMessage]:
        """Fetch without committing."""

    @abstractmethod
    def commit(self, group: str, message: BusMessage) -> None:
        """Acknowledge the message (and everything before it in its partition)."""

    @abstractmethod
    def retry(self, group: str, message: BusMessage) -> None:
        """Make an uncommitted message eligible for redelivery to this group."""

    @abstractmethod
    def health(self) -> bool: ...

    @abstractmethod
    def lag(self, group: str, topics: list[str]) -> int | None:
        """Total uncommitted messages for the group, or None when unavailable."""

    @abstractmethod
    def close(self) -> None: ...


def deterministic_partition(key: str | None, partitions: int) -> int:
    if key is None:
        return 0
    return zlib.crc32(key.encode()) % partitions


class InMemoryEventBus(EventBus):
    """Deterministic, single-process adapter for unit tests and local experiments."""

    backend = "memory"

    def __init__(self, partitions: int = 3) -> None:
        self.partitions = partitions
        self._lock = threading.RLock()
        self._log: dict[str, list[list[BusMessage]]] = {}
        self._position: dict[tuple[str, str, int], int] = defaultdict(int)
        self._committed: dict[tuple[str, str, int], int] = defaultdict(int)
        self.available = True
        self.fail_publishes = 0
        self.published: list[BusMessage] = []

    def ensure_topics(self, topics: list[str], partitions: int) -> None:
        with self._lock:
            for topic in topics:
                self._log.setdefault(topic, [[] for _ in range(partitions)])

    def _partitions_of(self, topic: str) -> list[list[BusMessage]]:
        return self._log.setdefault(topic, [[] for _ in range(self.partitions)])

    def publish(
        self, topic: str, key: str | None, value: bytes, headers: dict[str, str] | None = None
    ) -> BusMessage:
        with self._lock:
            if not self.available:
                raise PublishError("broker unavailable")
            if self.fail_publishes > 0:
                self.fail_publishes -= 1
                raise PublishError("injected publish failure")
            partitions = self._partitions_of(topic)
            index = deterministic_partition(key, len(partitions))
            message = BusMessage(
                topic=topic,
                value=value,
                key=key,
                headers=safe_headers(headers),
                partition=index,
                offset=len(partitions[index]),
                timestamp_ms=int(time.time() * 1000),
            )
            partitions[index].append(message)
            self.published.append(message)
            return message

    def consume(
        self, topics: list[str], group: str, max_messages: int = 1, timeout: float = 1.0
    ) -> list[BusMessage]:
        with self._lock:
            if not self.available:
                raise BusUnavailableError("broker unavailable")
            result: list[BusMessage] = []
            for topic in topics:
                for index, records in enumerate(self._partitions_of(topic)):
                    position = self._position[(group, topic, index)]
                    while position < len(records) and len(result) < max_messages:
                        result.append(records[position])
                        position += 1
                    self._position[(group, topic, index)] = position
            return result

    def commit(self, group: str, message: BusMessage) -> None:
        with self._lock:
            if message.partition is None or message.offset is None:
                raise ValueError("message has no partition/offset")
            key = (group, message.topic, message.partition)
            self._committed[key] = max(self._committed[key], message.offset + 1)

    def retry(self, group: str, message: BusMessage) -> None:
        with self._lock:
            if message.partition is None or message.offset is None:
                raise ValueError("message has no partition/offset")
            key = (group, message.topic, message.partition)
            self._position[key] = min(self._position[key], message.offset)

    def restart_consumer(self, group: str) -> None:
        """Simulate a consumer crash/restart: positions fall back to the committed offsets."""
        with self._lock:
            for key in list(self._position):
                if key[0] == group:
                    self._position[key] = self._committed[key]

    def health(self) -> bool:
        return self.available

    def lag(self, group: str, topics: list[str]) -> int | None:
        with self._lock:
            return sum(
                len(records) - self._committed[(group, topic, index)]
                for topic in topics
                for index, records in enumerate(self._partitions_of(topic))
            )

    def messages(self, topic: str) -> list[BusMessage]:
        with self._lock:
            return [m for records in self._partitions_of(topic) for m in records]

    def close(self) -> None:
        return None


class KafkaEventBus(EventBus):
    """Adapter for Kafka-compatible brokers (Redpanda locally) using confluent-kafka."""

    backend = "kafka"

    def __init__(self, settings: Settings) -> None:
        from confluent_kafka import Producer

        self.settings = settings
        self._servers = settings.KAFKA_BOOTSTRAP_SERVERS
        self._produce_timeout = settings.KAFKA_PRODUCE_TIMEOUT_SECONDS
        self._producer = Producer(
            {
                "bootstrap.servers": self._servers,
                "broker.address.family": "v4",
                "acks": "all",
                "enable.idempotence": True,
                "message.timeout.ms": int(self._produce_timeout * 1000),
                "socket.timeout.ms": int(self._produce_timeout * 1000),
                "linger.ms": 2,
                "client.id": "opspilot-producer",
            }
        )
        self._consumers: dict[str, Any] = {}
        self._subscribed: dict[str, tuple[str, ...]] = {}
        self._stop = threading.Event()
        self._poller = threading.Thread(target=self._poll, name="kafka-poll", daemon=True)
        self._poller.start()
        self._health_cache: tuple[float, bool] = (0.0, False)
        self._lag_cache: dict[tuple[str, tuple[str, ...]], tuple[float, int | None]] = {}

    def _poll(self) -> None:
        while not self._stop.is_set():
            self._producer.poll(0.05)

    def _admin(self) -> Any:
        from confluent_kafka.admin import AdminClient

        return AdminClient(
            {
                "bootstrap.servers": self._servers,
                "broker.address.family": "v4",
                "socket.timeout.ms": 3000,
            }
        )

    def ensure_topics(self, topics: list[str], partitions: int) -> None:
        from confluent_kafka import admin as kafka_admin

        admin = self._admin()
        futures = admin.create_topics(
            [
                kafka_admin.NewTopic(  # type: ignore[attr-defined]
                    topic, num_partitions=partitions, replication_factor=1
                )
                for topic in topics
            ]
        )
        for topic, future in futures.items():
            try:
                future.result(timeout=15)
            except Exception as exc:
                if "TOPIC_ALREADY_EXISTS" not in str(exc):
                    raise BusUnavailableError(f"cannot create topic {topic}") from exc

    def publish(
        self, topic: str, key: str | None, value: bytes, headers: dict[str, str] | None = None
    ) -> BusMessage:
        done = threading.Event()
        outcome: dict[str, Any] = {}

        def delivered(error: Any, message: Any) -> None:
            if error is not None:
                outcome["error"] = str(error)
            else:
                outcome["partition"] = message.partition()
                outcome["offset"] = message.offset()
            done.set()

        safe = safe_headers(headers)
        try:
            self._producer.produce(
                topic,
                value=value,
                key=key.encode() if key else None,
                headers=[(k, v.encode()) for k, v in safe.items()],
                on_delivery=delivered,
            )
        except Exception as exc:
            raise PublishError(f"produce failed: {type(exc).__name__}") from exc
        if not done.wait(self._produce_timeout + 1) or "error" in outcome:
            raise PublishError("record was not acknowledged by the broker")
        return BusMessage(
            topic=topic,
            value=value,
            key=key,
            headers=safe,
            partition=int(outcome["partition"]),
            offset=int(outcome["offset"]),
            timestamp_ms=int(time.time() * 1000),
        )

    def _consumer(self, group: str) -> Any:
        from confluent_kafka import Consumer

        consumer = self._consumers.get(group)
        if consumer is None:
            consumer = Consumer(
                {
                    "bootstrap.servers": self._servers,
                    "broker.address.family": "v4",
                    "group.id": group,
                    "enable.auto.commit": False,
                    "auto.offset.reset": "earliest",
                    "session.timeout.ms": 15000,
                    "socket.timeout.ms": 5000,
                    "client.id": f"opspilot-{group}",
                }
            )
            self._consumers[group] = consumer
        return consumer

    def consume(
        self, topics: list[str], group: str, max_messages: int = 1, timeout: float = 1.0
    ) -> list[BusMessage]:
        consumer = self._consumer(group)
        wanted = tuple(sorted(topics))
        if self._subscribed.get(group) != wanted:
            consumer.subscribe(list(wanted))
            self._subscribed[group] = wanted
        try:
            records = consumer.consume(num_messages=max_messages, timeout=timeout)
        except Exception as exc:
            raise BusUnavailableError(f"consume failed: {type(exc).__name__}") from exc
        result: list[BusMessage] = []
        for record in records:
            if record.error():
                if "PARTITION_EOF" in str(record.error()):
                    continue
                raise BusUnavailableError(f"consume error: {record.error().name()}")
            result.append(
                BusMessage(
                    topic=record.topic(),
                    value=record.value() or b"",
                    key=record.key().decode() if record.key() else None,
                    headers={k: v.decode() for k, v in (record.headers() or []) if v is not None},
                    partition=record.partition(),
                    offset=record.offset(),
                    timestamp_ms=record.timestamp()[1],
                )
            )
        return result

    def commit(self, group: str, message: BusMessage) -> None:
        from confluent_kafka import TopicPartition

        consumer = self._consumer(group)
        try:
            consumer.commit(
                offsets=[
                    TopicPartition(message.topic, message.partition or 0, (message.offset or 0) + 1)
                ],
                asynchronous=False,
            )
        except Exception as exc:
            raise BusUnavailableError(f"commit failed: {type(exc).__name__}") from exc

    def retry(self, group: str, message: BusMessage) -> None:
        from confluent_kafka import TopicPartition

        consumer = self._consumer(group)
        consumer.seek(TopicPartition(message.topic, message.partition or 0, message.offset or 0))

    def health(self) -> bool:
        checked, healthy = self._health_cache
        if time.monotonic() - checked < 2.0:
            return healthy
        try:
            self._admin().list_topics(timeout=2)
            healthy = True
        except Exception:
            healthy = False
        self._health_cache = (time.monotonic(), healthy)
        return healthy

    def lag(self, group: str, topics: list[str]) -> int | None:
        from confluent_kafka import Consumer, TopicPartition

        cache_key = (group, tuple(sorted(topics)))
        cached = self._lag_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 2.0:
            return cached[1]
        total: int | None
        try:
            probe = Consumer(
                {
                    "bootstrap.servers": self._servers,
                    "broker.address.family": "v4",
                    "group.id": group,
                    "enable.auto.commit": False,
                    "socket.timeout.ms": 3000,
                }
            )
            try:
                metadata = probe.list_topics(timeout=3)
                total = 0
                for topic in topics:
                    entry = metadata.topics.get(topic)
                    if entry is None or entry.error is not None:
                        continue
                    partitions = [TopicPartition(topic, p) for p in entry.partitions]
                    for committed in probe.committed(partitions, timeout=3):
                        low, high = probe.get_watermark_offsets(committed, timeout=3)
                        position = committed.offset if committed.offset >= 0 else low
                        total += max(0, high - position)
            finally:
                probe.close()
        except Exception:
            total = None
        self._lag_cache[cache_key] = (time.monotonic(), total)
        return total

    def close(self) -> None:
        self._stop.set()
        self._producer.flush(2)
        for consumer in self._consumers.values():
            try:
                consumer.close()
            except Exception:
                logger.debug("consumer close failed")


_BUSES: dict[str, EventBus] = {}


def get_bus(settings: Settings) -> EventBus:
    """One bus per process (a shared producer and cached consumers)."""
    key = f"{settings.EVENT_BUS_BACKEND}:{settings.KAFKA_BOOTSTRAP_SERVERS}"
    bus = _BUSES.get(key)
    if bus is None:
        bus = (
            InMemoryEventBus(settings.KAFKA_PARTITIONS)
            if settings.EVENT_BUS_BACKEND == "memory"
            else KafkaEventBus(settings)
        )
        _BUSES[key] = bus
    return bus
