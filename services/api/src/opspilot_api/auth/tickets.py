"""Single-use WebSocket tickets: random, short-lived, bound to user, workspace, and channel."""

import hashlib
import json
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError


def ticket_key(ticket: str) -> str:
    return "opspilot:wst:" + hashlib.sha256(ticket.encode()).hexdigest()


async def consume_ticket(
    redis: Redis, ticket: str, workspace_id: UUID, channel: str
) -> dict[str, Any] | None:
    """Atomically take the ticket (GETDEL). A reused, expired, or mismatched ticket yields ``None``.

    The ticket is consumed even when it is for another workspace or channel, so a stolen ticket
    cannot be probed repeatedly.
    """
    if not ticket or len(ticket) > 200:
        return None
    try:
        raw = await redis.getdel(ticket_key(ticket))
    except RedisError:
        return None
    if raw is None:
        return None
    try:
        claims: dict[str, Any] = json.loads(raw)
    except ValueError:
        return None
    if claims.get("workspace_id") != str(workspace_id) or claims.get("channel") != channel:
        return None
    return claims
