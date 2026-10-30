from datetime import datetime

from opspilot_api.worker.jobs import ping


def test_ping_job_returns_typed_payload() -> None:
    result = ping("hello")
    assert result["message"] == "hello"
    datetime.fromisoformat(result["handled_at"])
