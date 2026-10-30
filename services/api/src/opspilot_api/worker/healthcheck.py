"""Container health check: exits 0 only if this worker is registered in Redis."""

import sys

from rq import Worker

from opspilot_api.config import get_settings
from opspilot_api.worker.main import create_redis, worker_name


def is_registered() -> bool:
    connection = create_redis(get_settings())
    try:
        return any(w.name == worker_name() for w in Worker.all(connection=connection))
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        healthy = is_registered()
    except Exception as exc:
        print(f"worker unhealthy: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0 if healthy else 1)
