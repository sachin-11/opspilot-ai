"""Dependency-failure drill: ``python -m opspilot_api.maintenance.chaos --project P --env-file F``.

Stops PostgreSQL and then Redis in a *throwaway* Compose project, checks that the API degrades the
way the documentation says it does, restarts the dependency, and checks that the API recovers
without being restarted. Services are always started again, even when a check fails.

Never point it at a stack you care about: it stops real containers. It refuses unless the
Compose project name contains ``check``, ``chaos`` or ``drill`` and the API reports a
non-production environment. Requires ``AUTH_PROVIDER=local`` (it signs in with the dev provider).
"""

import argparse
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import httpx

from opspilot_api.observability.loadgen import local_login

SAFE_PROJECT_WORDS = ("check", "chaos", "drill")
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILURES.append(label)


class Stack:
    def __init__(self, project: str, env_file: str) -> None:
        self.base = ["docker", "compose", "-p", project, "--env-file", env_file]

    def run(self, *args: str) -> None:
        # Arguments come from this module and the operator's own command line.
        subprocess.run([*self.base, *args], check=True, capture_output=True, text=True)  # noqa: S603

    @contextmanager
    def down(self, service: str) -> Iterator[None]:
        self.run("stop", service)
        try:
            yield
        finally:
            self.run("start", service)


def wait_for(condition: Callable[[], bool], timeout: float, interval: float = 1.0) -> float | None:
    """Seconds until ``condition`` held, or None on timeout."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            if condition():
                return time.monotonic() - started
        except httpx.HTTPError:
            pass
        time.sleep(interval)
    return None


def ready_status(client: httpx.Client, api: str) -> tuple[int, dict[str, object]]:
    response = client.get(f"{api}/health/ready", timeout=15)
    return response.status_code, response.json()


def error_shape_is_clean(response: httpx.Response) -> bool:
    """A failure must be a short JSON error, never a stack trace or an HTML page."""
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and "error" in body and "Traceback" not in response.text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="throwaway Compose project name")
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--api-base", default="http://localhost:8300")
    parser.add_argument("--redirect-origin", default="http://localhost:3300")
    parser.add_argument("--recovery-timeout", type=float, default=60.0)
    args = parser.parse_args()
    if not any(word in args.project for word in SAFE_PROJECT_WORDS):
        raise SystemExit(
            f"refusing: project {args.project!r} must contain one of {SAFE_PROJECT_WORDS}"
        )

    api = args.api_base.rstrip("/")
    stack = Stack(args.project, args.env_file)
    client = httpx.Client(timeout=30)
    if client.get(f"{api}/api/v1/meta").json().get("environment") == "production":
        raise SystemExit("refusing to run against a production environment")

    token = local_login(client, f"{api}/api/v1", "owner", args.redirect_origin)
    auth = {"Authorization": f"Bearer {token}"}
    suffix = uuid.uuid4().hex[:8]
    workspace = client.post(
        f"{api}/api/v1/workspaces", json={"name": f"chaos {suffix}"}, headers=auth
    ).json()["id"]
    incident = client.post(
        f"{api}/api/v1/workspaces/{workspace}/incidents",
        json={"title": "chaos drill", "severity": "sev3"},
        headers=auth,
    ).json()["id"]
    incidents_url = f"{api}/api/v1/workspaces/{workspace}/incidents"
    investigate_url = f"{incidents_url}/{incident}/investigations"

    code, _ = ready_status(client, api)
    check(
        "baseline: ready and reads work",
        code == 200 and client.get(incidents_url, headers=auth).status_code == 200,
    )

    print("\n--- PostgreSQL stopped ---")
    with stack.down("postgres"):
        degraded = wait_for(lambda: ready_status(client, api)[0] == 503, 30)
        check(
            "readiness reports degraded",
            degraded is not None,
            f"{degraded:.1f}s" if degraded is not None else "never",
        )
        _, body = ready_status(client, api)
        deps = body.get("dependencies", {})
        check(
            "readiness names postgres as down and redis as up",
            isinstance(deps, dict) and deps.get("postgres") == "down" and deps.get("redis") == "up",
            str(deps),
        )
        check(
            "liveness stays 200 (the process is not restarted for a dependency outage)",
            client.get(f"{api}/health/live").status_code == 200,
        )
        started = time.monotonic()
        response = client.get(incidents_url, headers=auth, timeout=40)
        took = time.monotonic() - started
        check(
            "an authenticated read fails fast as 503 with Retry-After and a clean error",
            response.status_code == 503
            and "retry-after" in response.headers
            and took < 20
            and error_shape_is_clean(response),
            f"HTTP {response.status_code} in {took:.1f}s",
        )
    recovered = wait_for(lambda: ready_status(client, api)[0] == 200, args.recovery_timeout)
    check(
        "API recovers by itself after PostgreSQL returns",
        recovered is not None,
        f"{recovered:.1f}s" if recovered is not None else "no recovery",
    )
    check(
        "reads work again without restarting the API",
        client.get(incidents_url, headers=auth).status_code == 200,
    )

    print("\n--- Redis stopped ---")
    with stack.down("redis"):
        degraded = wait_for(lambda: ready_status(client, api)[0] == 503, 30)
        check(
            "readiness reports degraded",
            degraded is not None,
            f"{degraded:.1f}s" if degraded is not None else "never",
        )
        _, body = ready_status(client, api)
        deps = body.get("dependencies", {})
        check(
            "readiness names redis as down and postgres as up",
            isinstance(deps, dict) and deps.get("redis") == "down" and deps.get("postgres") == "up",
            str(deps),
        )
        read = client.get(incidents_url, headers=auth, timeout=40)
        check(
            "reads still work (low-risk rate limits fail open)",
            read.status_code == 200,
            f"HTTP {read.status_code}",
        )
        started = time.monotonic()
        started_investigation = client.post(
            investigate_url, json={"trigger": "manual"}, headers=auth, timeout=40
        )
        took = time.monotonic() - started
        check(
            "starting an investigation is refused, not accepted (high-risk rules fail closed)",
            started_investigation.status_code in {429, 503}
            and took < 20
            and error_shape_is_clean(started_investigation),
            f"HTTP {started_investigation.status_code} in {took:.1f}s",
        )
    recovered = wait_for(lambda: ready_status(client, api)[0] == 200, args.recovery_timeout)
    check(
        "API recovers by itself after Redis returns",
        recovered is not None,
        f"{recovered:.1f}s" if recovered is not None else "no recovery",
    )
    again = client.post(investigate_url, json={"trigger": "manual"}, headers=auth)
    check(
        "the investigation can be started again",
        again.status_code == 202,
        f"HTTP {again.status_code}",
    )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: " + "; ".join(FAILURES))
        sys.exit(1)
    print("chaos drill passed")


if __name__ == "__main__":
    main()
