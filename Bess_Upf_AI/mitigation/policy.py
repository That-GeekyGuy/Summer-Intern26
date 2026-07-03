from __future__ import annotations
from enum import Enum


class ActionClass(str, Enum):
    HPA_SCALE_UP   = "hpa_scale_up"
    XDP_RATE_LIMIT = "xdp_rate_limit"
    PFCP_REROUTE   = "pfcp_reroute"
    NO_ACTION      = "no_action"


class TrustLevel(str, Enum):
    OBSERVE   = "observe"
    RECOMMEND = "recommend"
    APPROVE   = "approve"
    AUTO      = "auto"


TRUST_LEVELS: dict[ActionClass, TrustLevel] = {
    ActionClass.HPA_SCALE_UP:   TrustLevel.OBSERVE,
    ActionClass.XDP_RATE_LIMIT: TrustLevel.OBSERVE,
    ActionClass.PFCP_REROUTE:   TrustLevel.OBSERVE,
}

MIN_CONFIDENCE = 0.6

_DROP_CHANNELS = {"port_dropped_N6_rx_rate", "port_dropped_N3_rx_rate"}
_PFCP_CHANNELS = {"pfcp_sessions_total", "pfcp_session_setup_rate"}


def classify_anomaly(event: dict) -> tuple[ActionClass, TrustLevel]:
    score = float(event.get("anomaly_score") or 0.0)
    if score < MIN_CONFIDENCE:
        return ActionClass.NO_ACTION, TrustLevel.OBSERVE
    top_ch: list[str] = event.get("top_anomalous_channels") or []
    if any(ch in _DROP_CHANNELS for ch in top_ch):
        action = ActionClass.XDP_RATE_LIMIT
    elif any(ch in _PFCP_CHANNELS for ch in top_ch):
        action = ActionClass.PFCP_REROUTE
    else:
        action = ActionClass.HPA_SCALE_UP
    return action, TRUST_LEVELS.get(action, TrustLevel.OBSERVE)
