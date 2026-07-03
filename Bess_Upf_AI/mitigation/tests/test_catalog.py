import pytest
from mitigation.catalog import execute, rollback, ActuatorResult
from mitigation.policy import ActionClass


def test_hpa_dry_run_returns_result():
    result = execute(ActionClass.HPA_SCALE_UP, "upf-1", dry_run=True)
    assert isinstance(result, ActuatorResult)
    assert result.success
    assert result.dry_run
    assert result.action_class == ActionClass.HPA_SCALE_UP
    assert result.upf_id == "upf-1"
    assert result.rollback_token.startswith("hpa:upf-1:")


def test_xdp_dry_run():
    result = execute(ActionClass.XDP_RATE_LIMIT, "upf-2", dry_run=True)
    assert result.success
    assert result.action_class == ActionClass.XDP_RATE_LIMIT


def test_pfcp_dry_run():
    result = execute(ActionClass.PFCP_REROUTE, "upf-3", dry_run=True)
    assert result.success
    assert result.action_class == ActionClass.PFCP_REROUTE


def test_no_action_returns_no_op():
    result = execute(ActionClass.NO_ACTION, "upf-4", dry_run=True)
    assert result.action_class == ActionClass.NO_ACTION
    assert result.message == "no_action"


def test_rollback_does_not_raise():
    rollback("hpa:upf-1:some-id")
