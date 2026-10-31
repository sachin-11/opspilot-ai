"""Deterministic load generator and benchmark (development/test only).

    python -m opspilot_api.observability.loadgen --events 2000 --rate 200 --duplicate-percent 10

Runs against a *running* stack over HTTP: creates a throwaway workspace, services, and signed
webhook sources, sends a seeded, reproducible plan of signals (valid, duplicate, invalid), waits
for the consumers to drain, and writes measured results (JSON) to ``benchmark-results/`` (git
ignored). Nothing here is a claim about production capacity; it reports what this run measured.
"""

import argparse
import base64
import hashlib
import hmac
import json
import random
import secrets
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

TIMESTAMP_HEADER = "X-OpsPilot-Timestamp"
SIGNATURE_HEADER = "X-OpsPilot-Signature"


@dataclass(frozen=True)
class PlannedEvent:
    service_index: int
    body: bytes
    kind: str  # valid | duplicate | invalid
    event_id: str


def build_plan(
    *,
    events: int,
    services: int,
    duplicate_percent: int,
    invalid_percent: int,
    fingerprints: int,
    seed: int,
) -> list[PlannedEvent]:
    """Reproducible: the same arguments always yield the same sequence of payload shapes."""
    rng = random.Random(seed)  # noqa: S311 - benchmark data, not security
    plan: list[PlannedEvent] = []
    issued: list[PlannedEvent] = []
    for index in range(events):
        roll = rng.randrange(100)
        if issued and roll < duplicate_percent:
            original = issued[rng.randrange(len(issued))]
            plan.append(
                PlannedEvent(original.service_index, original.body, "duplicate", original.event_id)
            )
            continue
        service_index = rng.randrange(services)
        event_id = f"bench-{seed}-{index}"
        if roll >= 100 - invalid_percent:
            payload: dict[str, Any] = {"type": "mystery", "event_id": event_id}
            kind = "invalid"
        else:
            signature = rng.randrange(
                fingerprints
            )  # skewed reuse => correlation into few incidents
            payload = {
                "type": "alert",
                "event_id": event_id,
                "title": f"Benchmark failure family {signature}",
                "message": f"synthetic alert {index}",
                "severity": ["high", "medium", "critical"][signature % 3],
                "occurred_at": "@NOW@",
                "error_signature": f"bench-family-{signature}",
            }
            kind = "valid"
        item = PlannedEvent(service_index, json.dumps(payload).encode(), kind, event_id)
        plan.append(item)
        if kind == "valid":
            issued.append(item)
    return plan


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = round(fraction * len(ordered) + 0.5)
    return ordered[min(len(ordered) - 1, max(0, rank - 1))]


def sign(secret: str, body: bytes) -> dict[str, str]:
    stamp = str(int(time.time()))
    digest = hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {TIMESTAMP_HEADER: stamp, SIGNATURE_HEADER: digest, "Content-Type": "application/json"}


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def local_login(client: httpx.Client, api: str, user: str, redirect_origin: str) -> str:
    """Sign in through the development-only local identity provider (authorization code + PKCE)."""
    redirect_uri = f"{redirect_origin.rstrip('/')}/api/auth/callback"
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(12)
    authorize = client.get(
        f"{api}/auth/local/authorize",
        params={
            "redirect_uri": redirect_uri,
            "state": state,
            "nonce": secrets.token_urlsafe(12),
            "code_challenge": pkce_challenge(verifier),
            "login_hint": user,
        },
        follow_redirects=False,
    )
    code = parse_qs(urlparse(authorize.headers.get("location", "")).query).get("code")
    if authorize.status_code not in (302, 307) or not code:
        raise SystemExit(
            f"local sign-in failed (HTTP {authorize.status_code}); is AUTH_PROVIDER=local and is "
            f"{redirect_origin} in CORS_ORIGINS? Otherwise pass --auth-token."
        )
    token = client.post(
        f"{api}/auth/local/token",
        data={
            "grant_type": "authorization_code",
            "code": code[0],
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        },
    )
    token.raise_for_status()
    return str(token.json()["access_token"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://localhost:8300")
    parser.add_argument(
        "--auth-token",
        default=None,
        help="bearer token for the control calls; default: sign in with the local dev provider",
    )
    parser.add_argument("--local-user", default="owner", help="local dev provider user")
    parser.add_argument("--redirect-origin", default="http://localhost:3300")
    parser.add_argument("--events", type=int, default=1000)
    parser.add_argument("--rate", type=float, default=100.0, help="events/second; 0 = burst")
    parser.add_argument("--services", type=int, default=3)
    parser.add_argument("--duplicate-percent", type=int, default=10)
    parser.add_argument("--invalid-percent", type=int, default=2)
    parser.add_argument("--fingerprints", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--drain-timeout", type=float, default=180.0)
    parser.add_argument("--output-dir", type=Path, default=Path("benchmark-results"))
    args = parser.parse_args()

    api = args.api_base.rstrip("/") + "/api/v1"
    client = httpx.Client(timeout=30, limits=httpx.Limits(max_connections=args.concurrency * 2))
    environment = client.get(f"{api}/meta").json().get("environment")
    if environment == "production":
        raise SystemExit("refusing to run the load generator against a production environment")

    # Control calls (workspace, services, sources, read-back) are authenticated; the signed webhook
    # deliveries below deliberately are not, exactly like a real monitoring system.
    token = args.auth_token or local_login(client, api, args.local_user, args.redirect_origin)
    control = httpx.Client(timeout=30, headers={"Authorization": f"Bearer {token}"})
    suffix = uuid.uuid4().hex[:8]
    created_workspace = control.post(
        f"{api}/workspaces", json={"name": f"bench {suffix}", "slug": f"bench-{suffix}"}
    )
    created_workspace.raise_for_status()
    workspace_id = created_workspace.json()["id"]
    sources: list[tuple[str, str]] = []
    for index in range(args.services):
        service = control.post(
            f"{api}/workspaces/{workspace_id}/services",
            json={
                "name": f"Bench Service {index}",
                "slug": f"bench-svc-{index}-{suffix}",
                "environment": "production",
            },
        ).json()
        created = control.post(
            f"{api}/workspaces/{workspace_id}/observability/sources",
            json={
                "name": f"bench-source-{index}",
                "provider": "generic_webhook",
                "service_id": service["id"],
                "environment": "production",
            },
        )
        created.raise_for_status()
        body = created.json()
        sources.append((body["public_id"], body["webhook_secret"]))

    plan = build_plan(
        events=args.events, services=args.services, duplicate_percent=args.duplicate_percent,
        invalid_percent=args.invalid_percent, fingerprints=args.fingerprints, seed=args.seed,
    )  # fmt: skip
    counts: dict[str, int] = {}
    latencies: list[float] = []
    started = time.perf_counter()

    def send(position: int, item: PlannedEvent) -> tuple[str, float]:
        if args.rate > 0:
            wait = started + position / args.rate - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
        public_id, secret = sources[item.service_index]
        body = item.body.replace(b'"@NOW@"', json.dumps(datetime.now(UTC).isoformat()).encode())
        began = time.perf_counter()
        try:
            response = client.post(
                f"{api}/ingest/webhooks/{public_id}", content=body, headers=sign(secret, body)
            )
            label = str(response.status_code)
        except httpx.HTTPError:
            label = "transport_error"
        return label, (time.perf_counter() - began) * 1000

    lag_samples: list[int] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(send, i, item) for i, item in enumerate(plan)]
        for future in futures:
            label, latency = future.result()
            counts[label] = counts.get(label, 0) + 1
            latencies.append(latency)
    send_seconds = time.perf_counter() - started

    pipeline_url = f"{api}/workspaces/{workspace_id}/observability/pipeline?window_minutes=60"
    deadline = time.time() + args.drain_timeout
    status: dict[str, Any] = {}
    while time.time() < deadline:
        status = control.get(pipeline_url).json()
        if status.get("consumer_lag") is not None:
            lag_samples.append(int(status["consumer_lag"]))
        settled = status["processed"] + status["duplicates"] + status["failures"]
        if (
            settled >= status["accepted"]
            and status["accepted"] > 0
            and status.get("consumer_lag") in (0, None)
        ):
            break
        time.sleep(1)
    drain_seconds = time.perf_counter() - started

    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = control.get(
            f"{api}/workspaces/{workspace_id}/observability/signals?limit=200"
            + (f"&cursor={cursor}" if cursor else "")
        ).json()
        items += page["items"]
        cursor = page["next_cursor"]
        if not cursor:
            break
    e2e: list[float] = []
    processed_times: list[float] = []
    received_times: list[float] = []
    for item in items:
        received = datetime.fromisoformat(item["received_at"]).timestamp()
        received_times.append(received)
        if item["status"] == "processed" and item["processed_at"]:
            done = datetime.fromisoformat(item["processed_at"]).timestamp()
            e2e.append((done - received) * 1000)
            processed_times.append(done)
    by_status: dict[str, int] = {}
    for item in items:
        by_status[item["status"]] = by_status.get(item["status"], 0) + 1
    injected_duplicates = sum(1 for item in plan if item.kind == "duplicate")
    suppressed = counts.get("200", 0) + by_status.get("duplicate", 0)
    non_ok = sum(v for k, v in counts.items() if k not in {"200", "202"})
    processing_window = (max(processed_times) - min(received_times)) if processed_times else 0.0
    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "parameters": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
        "environment": environment,
        "http_status_counts": counts,
        "events_sent": len(plan),
        "send_seconds": round(send_seconds, 3),
        "drain_seconds": round(drain_seconds, 3),
        "accepted_events_per_second": round((counts.get("202", 0)) / send_seconds, 2),
        "processed_events_per_second": (
            round(len(e2e) / processing_window, 2) if processing_window else None
        ),
        "request_latency_ms": {
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
        },
        "end_to_end_latency_ms": {
            "samples": len(e2e), "p50": percentile(e2e, 0.5), "p95": percentile(e2e, 0.95),
            "p99": percentile(e2e, 0.99), "mean": statistics.fmean(e2e) if e2e else None,
        },
        "receipts_by_status": by_status,
        "duplicates": {
            "injected": injected_duplicates, "suppressed": suppressed,
            "suppression_rate": (
                round(suppressed / injected_duplicates, 4) if injected_duplicates else None
            ),
        },
        "error_rate": round(non_ok / len(plan), 4),
        "dead_letter_rate": round(by_status.get("dead_lettered", 0) / max(1, len(items)), 4),
        "consumer_lag": {
            "max_observed": max(lag_samples) if lag_samples else None,
            "final": status.get("consumer_lag"),
        },
        "incidents_created": status.get("incidents_created"),
        "events_correlated": status.get("events_correlated"),
        "workspace_id": workspace_id,
    }  # fmt: skip
    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / f"pipeline-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    print(f"\nwritten to {out}")


if __name__ == "__main__":
    main()
