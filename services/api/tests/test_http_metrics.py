from uuid import uuid4

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from opspilot_api.http_metrics import UNMATCHED, HttpMetricsMiddleware


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_requests_are_counted_by_route_template_and_timed(client: TestClient) -> None:
    before = sample(
        "opspilot_http_requests_total", method="GET", route="/health/live", status="200"
    )
    for _ in range(3):
        assert client.get("/health/live").status_code == 200
    after = sample("opspilot_http_requests_total", method="GET", route="/health/live", status="200")
    assert after - before == 3
    assert (
        sample("opspilot_http_request_duration_seconds_count", method="GET", route="/health/live")
        >= 3
    )
    assert sample("opspilot_http_requests_in_progress") == 0


def test_path_parameters_never_become_label_values(client: TestClient) -> None:
    for _ in range(2):
        client.get(f"/api/v1/workspaces/{uuid4()}/incidents")  # 401 without a token
    routes = {
        sample_.labels["route"]
        for family in REGISTRY.collect()
        if family.name == "opspilot_http_requests"
        for sample_ in family.samples
    }
    assert any("{workspace_id}" in route for route in routes)
    assert not any(len(route) > 80 or route.count("-") >= 4 for route in routes)


def test_unmatched_paths_share_one_label(client: TestClient) -> None:
    labels = {"method": "GET", "route": UNMATCHED, "status": "404"}
    before = sample("opspilot_http_requests_total", **labels)
    for name in ("a", "b", "c"):
        assert client.get(f"/no-such-path/{name}").status_code == 404
    assert sample("opspilot_http_requests_total", **labels) - before == 3


def test_an_unhandled_error_is_recorded_as_a_server_error() -> None:
    async def boom(_request: Request) -> Response:
        raise RuntimeError("expected failure")

    app = Starlette(routes=[Route("/boom-metrics-test", boom)])
    app.add_middleware(HttpMetricsMiddleware)
    labels = {"method": "GET", "route": "/boom-metrics-test", "status": "500"}
    before = sample("opspilot_http_requests_total", **labels)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/boom-metrics-test").status_code == 500
    assert sample("opspilot_http_requests_total", **labels) - before == 1
    assert sample("opspilot_http_requests_in_progress") == 0
