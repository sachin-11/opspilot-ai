import json
from pathlib import Path


def test_golden_dataset_has_required_scenarios_and_safety_expectations() -> None:
    path = Path(__file__).parent / "fixtures" / "investigation_golden.json"
    fixtures = json.loads(path.read_text(encoding="utf-8"))
    assert len(fixtures) >= 12
    assert {item["id"] for item in fixtures} >= {
        "database-connection-exhaustion",
        "insufficient-evidence",
        "contradictory-evidence",
        "prompt-injection-log",
        "secret-bearing-log",
    }
    for item in fixtures:
        assert item["required_evidence"]
        assert item["acceptable_root_causes"]
        assert item["forbidden_claims"]
