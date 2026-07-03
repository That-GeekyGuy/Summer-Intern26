from __future__ import annotations
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from mitigation.policy import ActionClass

log = logging.getLogger(__name__)


@dataclass
class ActuatorResult:
    action_id:      str
    action_class:   ActionClass
    upf_id:         str
    dry_run:        bool
    success:        bool
    message:        str
    rollback_token: str


def _sim_hpa(upf_id: str, dry_run: bool) -> ActuatorResult:
    action_id = str(uuid.uuid4())
    if not dry_run:
        log.info("HPA scale-up: upf=%s", upf_id)
        time.sleep(0.05)
    return ActuatorResult(
        action_id=action_id,
        action_class=ActionClass.HPA_SCALE_UP,
        upf_id=upf_id,
        dry_run=dry_run,
        success=True,
        message="hpa_scale_up_simulated",
        rollback_token=f"hpa:{upf_id}:{action_id}",
    )


def _sim_xdp(upf_id: str, dry_run: bool) -> ActuatorResult:
    action_id = str(uuid.uuid4())
    if not dry_run:
        log.info("XDP rate-limit: upf=%s", upf_id)
        time.sleep(0.05)
    return ActuatorResult(
        action_id=action_id,
        action_class=ActionClass.XDP_RATE_LIMIT,
        upf_id=upf_id,
        dry_run=dry_run,
        success=True,
        message="xdp_rate_limit_simulated",
        rollback_token=f"xdp:{upf_id}:{action_id}",
    )


def _sim_pfcp(upf_id: str, dry_run: bool) -> ActuatorResult:
    action_id = str(uuid.uuid4())
    if not dry_run:
        log.info("PFCP reroute: upf=%s", upf_id)
        time.sleep(0.05)
    return ActuatorResult(
        action_id=action_id,
        action_class=ActionClass.PFCP_REROUTE,
        upf_id=upf_id,
        dry_run=dry_run,
        success=True,
        message="pfcp_reroute_simulated",
        rollback_token=f"pfcp:{upf_id}:{action_id}",
    )


def _no_op(upf_id: str, dry_run: bool) -> ActuatorResult:
    return ActuatorResult(
        action_id=str(uuid.uuid4()),
        action_class=ActionClass.NO_ACTION,
        upf_id=upf_id,
        dry_run=dry_run,
        success=True,
        message="no_action",
        rollback_token="",
    )


_CATALOG: dict[ActionClass, Callable[[str, bool], ActuatorResult]] = {
    ActionClass.HPA_SCALE_UP:   _sim_hpa,
    ActionClass.XDP_RATE_LIMIT: _sim_xdp,
    ActionClass.PFCP_REROUTE:   _sim_pfcp,
    ActionClass.NO_ACTION:      _no_op,
}


def execute(action_class: ActionClass, upf_id: str, dry_run: bool = True) -> ActuatorResult:
    fn = _CATALOG.get(action_class)
    if fn is None:
        raise ValueError(f"no actuator for {action_class!r}")
    return fn(upf_id, dry_run)


def rollback(rollback_token: str) -> None:
    if not rollback_token:
        log.warning("rollback called with empty token — skipping")
        return
    log.info("Rollback requested: token=%s", rollback_token)
