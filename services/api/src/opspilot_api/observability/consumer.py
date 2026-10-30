"""Raw/normalized consumers: ``python -m opspilot_api.observability.consumer --role raw``.

Each role has its own consumer group, so the two responsibilities scale and fail independently.
"""

import argparse
import asyncio
import logging
import signal
import time

from prometheus_client import start_http_server

from opspilot_api.config import Settings, get_settings
from opspilot_api.infrastructure import create_db_engine, create_session_factory
from opspilot_api.logging_config import configure_logging
from opspilot_api.observability.backpressure import NORMALIZED_GROUP, RAW_GROUP, consumer_lag
from opspilot_api.observability.bus import BusError, EventBus, Topics, get_bus
from opspilot_api.observability.pipeline import (
    Handler,
    PipelineContext,
    handle_normalized,
    handle_raw,
    process_message,
)
from opspilot_api.observability.telemetry import configure_tracing

logger = logging.getLogger("opspilot_api.observability.consumer")


def role_config(role: str, topics: Topics) -> tuple[str, str, Handler]:
    if role == "raw":
        return RAW_GROUP, topics.raw, handle_raw
    return NORMALIZED_GROUP, topics.normalized, handle_normalized


async def ensure_topics(settings: Settings, bus: EventBus, attempts: int = 30) -> None:
    topics = Topics.from_settings(settings).all()
    for attempt in range(1, attempts + 1):
        try:
            await asyncio.to_thread(bus.ensure_topics, topics, settings.KAFKA_PARTITIONS)
            return
        except BusError:
            logger.warning("broker not ready; waiting for topics", extra={"attempt": attempt})
            await asyncio.sleep(2)
    raise RuntimeError("broker did not become ready")


async def consume_loop(
    ctx: PipelineContext,
    role: str,
    stop: asyncio.Event,
    max_messages: int | None = None,
    poll_seconds: float = 1.0,
) -> int:
    """Consume until ``stop`` is set (or ``max_messages`` were handled). Returns handled count."""
    group, topic, handler = role_config(role, ctx.topics)
    handled = 0
    last_lag = 0.0
    while not stop.is_set() and (max_messages is None or handled < max_messages):
        try:
            messages = await asyncio.to_thread(ctx.bus.consume, [topic], group, 10, poll_seconds)
        except BusError:
            logger.warning("broker unavailable while consuming")
            await asyncio.sleep(2)
            continue
        for message in messages:
            try:
                await process_message(ctx, group, message, handler, role)
                handled += 1
            except Exception as exc:
                # Could not even dead-letter (e.g. database down): leave uncommitted and retry.
                logger.error(
                    "message left uncommitted for redelivery",
                    extra={"stage": role, "error_class": type(exc).__name__},
                )
                await asyncio.to_thread(ctx.bus.retry, group, message)
                await asyncio.sleep(2)
                break
        if time.monotonic() - last_lag > 5:
            last_lag = time.monotonic()
            await asyncio.to_thread(consumer_lag, ctx.bus, ctx.topics)
    return handled


async def drain(ctx: PipelineContext, role: str, poll_seconds: float = 0.2) -> int:
    """Process everything currently available, then return (tests, benchmarks, smoke runs)."""
    group, topic, handler = role_config(role, ctx.topics)
    handled = 0
    while True:
        messages = await asyncio.to_thread(ctx.bus.consume, [topic], group, 50, poll_seconds)
        if not messages:
            return handled
        for message in messages:
            await process_message(ctx, group, message, handler, role)
            handled += 1


async def run(settings: Settings, role: str) -> None:
    bus = get_bus(settings)
    engine = create_db_engine(settings)
    ctx = PipelineContext(settings, bus, create_session_factory(engine))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: stop.set())
    try:
        await ensure_topics(settings, bus)
        logger.info("consumer started", extra={"stage": role})
        await consume_loop(ctx, role, stop)
    finally:
        bus.close()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=["raw", "normalized"], required=True)
    args = parser.parse_args()
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL.value)
    configure_tracing(f"opspilot-{args.role}-consumer")
    start_http_server(settings.OBS_METRICS_PORT + (1 if args.role == "normalized" else 0))
    asyncio.run(run(settings, args.role))


if __name__ == "__main__":
    main()
