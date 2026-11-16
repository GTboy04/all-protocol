import pytest

from all_protocol.application.recovery import RecoveryPolicy


def test_policy_default_backoff_and_portable_fields():
    policy = RecoveryPolicy(True, 500, 1500, 4, True)
    assert [policy.delay(attempt) for attempt in range(1, 5)] == [.5, 1, 1.5, 1.5]
    assert RecoveryPolicy.from_fields(policy.fields()) == policy
    assert RecoveryPolicy().fields() == {}
    assert RecoveryPolicy.from_fields({"host": "127.0.0.1"}) == RecoveryPolicy()


@pytest.mark.parametrize("fields", (
    {"recovery_enabled": True}, {"recovery_enabled": 2}, {"recovery_restore_reads": "yes"},
    {"recovery_interval_ms": 0}, {"recovery_max_interval_ms": 500},
    {"recovery_max_attempts": 0}, {"recovery_max_attempts": 101},
    {"recovery_unknown": 1}, {"recovery_interval_ms": "1000"},
))
def test_policy_rejects_malformed_fields(fields):
    with pytest.raises(ValueError):
        RecoveryPolicy.from_fields(fields)
