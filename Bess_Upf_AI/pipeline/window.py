# pipeline/window.py
from collections import deque

ML_CHANNELS: list[str] = [
    "pfcp_sessions_total",
    "port_bytes_N3_rx_rate",
    "port_bytes_N6_tx_rate",
    "port_pkts_N3_rx_rate",
    "port_dropped_N3_rx_rate",
    "port_dropped_N6_rx_rate",
    "go_goroutines",
    "go_heap_alloc_bytes",
    "process_cpu_rate",
    "upf_sim_scenario_normal",
    "upf_sim_scenario_flatline",
    "upf_sim_scenario_packet_drop_surge",
    "port_pkts_N6_tx_rate",
    "port_dropped_N6_rx",
]

WINDOW_SIZE: int = 60


class WindowState:
    """
    Per-UPF sliding window. One deque per ML channel, maxlen=WINDOW_SIZE.
    Bytewax checkpoints this via pickle to SQLite on each recovery interval.
    """

    def __init__(self) -> None:
        self._channels: dict[str, deque[float]] = {
            ch: deque(maxlen=WINDOW_SIZE) for ch in ML_CHANNELS
        }

    def update(self, msg: dict) -> "WindowState":
        for ch in ML_CHANNELS:
            val = msg.get(ch)
            if val is None:
                val = 0.0
            self._channels[ch].append(float(val))
        return self

    def is_full(self) -> bool:
        return all(len(q) == WINDOW_SIZE for q in self._channels.values())

    def to_array(self) -> list[list[float]]:
        """Returns list of shape (len(ML_CHANNELS), WINDOW_SIZE)."""
        return [list(self._channels[ch]) for ch in ML_CHANNELS]
