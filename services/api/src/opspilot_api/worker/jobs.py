"""Background jobs. Module 0 only ships a smoke-test job."""

import logging
from datetime import UTC, datetime

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class PingResult(BaseModel):
    message: str
    handled_at: datetime


def ping(message: str = "pong") -> dict[str, str]:
    """Smoke-test job proving the worker can pick up and execute work.

    RQ serialises results, so a plain JSON-friendly dict is returned.
    """
    result = PingResult(message=message, handled_at=datetime.now(UTC))
    logger.info("ping job executed", extra={"job_message": message})
    return {"message": result.message, "handled_at": result.handled_at.isoformat()}
