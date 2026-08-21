from __future__ import annotations

from inumi.gateway.domain.data_policy import DataMinimizer


def test_masks_sensitive_fields_by_default():
    minimizer = DataMinimizer()
    rows = [{"username": "svc_app", "password_hash": "abc123", "cpu_percent": 92}]
    clean, masked, truncated = minimizer.apply(rows)
    assert clean[0]["password_hash"] == "***MASKED***"
    assert clean[0]["cpu_percent"] == 92
    assert "password_hash" in masked
    assert truncated is False


def test_truncates_rows_over_the_cap():
    minimizer = DataMinimizer()
    rows = [{"i": i} for i in range(500)]
    clean, _masked, truncated = minimizer.apply(rows, max_rows=10)
    assert len(clean) == 10
    assert truncated is True


def test_never_returns_select_star_worth_of_pii_unmasked():
    minimizer = DataMinimizer()
    rows = [
        {
            "account_number": "0123456789",
            "bvn": "12345678901",
            "email": "person@example.com",
            "phone": "+2348000000000",
        }
    ]
    clean, masked, _ = minimizer.apply(rows)
    assert all(v == "***MASKED***" for v in clean[0].values())
    assert set(masked) == {"account_number", "bvn", "email", "phone"}
