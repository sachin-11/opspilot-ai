import pytest

from .conftest import make_settings


def test_retention_is_off_by_default() -> None:
    settings = make_settings()
    assert settings.RETENTION_SIGNAL_RECEIPT_DAYS == 0
    assert settings.RETENTION_DEAD_LETTER_DAYS == 0


def test_a_retention_period_must_be_off_or_at_least_a_week() -> None:
    # Shorter windows would let a provider redelivery be processed again as a new event.
    for field in ("RETENTION_SIGNAL_RECEIPT_DAYS", "RETENTION_DEAD_LETTER_DAYS"):
        for bad in (1, 6):
            with pytest.raises(ValueError, match=r"0 \(keep forever\) or at least 7 days"):
                make_settings(**{field: bad})
        assert getattr(make_settings(**{field: 7}), field) == 7
        assert getattr(make_settings(**{field: 0}), field) == 0
    with pytest.raises(ValueError):
        make_settings(RETENTION_BATCH_SIZE=1)
