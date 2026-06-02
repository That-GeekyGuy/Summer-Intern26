"""
UPF metrics simulator.

Traffic pattern (30-second repeating cycle):
  0–15 s   — normal  : ~10 KB/s drops, well below 0.5 MB/s alert threshold
  15–30 s  — spike   : ~3 MB/s drops (30% loss), fires HighBytesDropped after 10 s

Rate window in rules is [15s], scrape interval is 5s → 3 data points per window.
Each phase is 15s so the window sees only one phase at a time → clean on/off.
"""

import math
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

counters = {
    ("port_bytes_count",   "rx", "N3"): 817_404,
    ("port_bytes_count",   "tx", "N6"): 842_716,
    ("port_bytes_count",   "rx", "N6"): 817_464,
    ("port_bytes_count",   "tx", "N3"): 842_870,
    ("port_packets_count", "rx", "N3"): 13_622,
    ("port_packets_count", "tx", "N6"): 13_977,
    ("port_packets_count", "rx", "N6"): 13_623,
    ("port_packets_count", "tx", "N3"): 13_979,
    ("port_dropped_count", "rx", "N3"): 100,
    ("port_dropped_count", "rx", "N6"): 101,
    ("port_dropped_count", "tx", "N3"): 102,
    ("port_dropped_count", "tx", "N6"): 304,
}
lock = threading.Lock()

CYCLE        = 30    # total cycle length in seconds
SPIKE_START  = 15    # spike begins 15 s into the cycle (lasts 15 s)
UPDATE_EVERY = 5     # counter update interval in seconds
AVG_PKT_SIZE = 1_400

# ~10 MB/s base throughput: 50 MB per 5-second tick
BASE_BYTES = 50_000_000


def _jitter(scale=0.03):
    return 1.0 + random.uniform(-scale, scale)


def _sine_wave(t, period=60, amplitude=0.08):
    """Slow sine so throughput graphs look natural."""
    return 1.0 + amplitude * math.sin(2 * math.pi * t / period)


def simulate():
    start = time.time()
    while True:
        t        = time.time() - start
        phase    = t % CYCLE
        in_spike = phase >= SPIKE_START

        tick = BASE_BYTES * _sine_wave(t) * _jitter()

        if in_spike:
            # 30% packet loss → drop rate ~3 MB/s >> 0.5 MB/s threshold
            rx_n3 = int(tick)
            tx_n6 = int(tick * 0.70 * _jitter(0.01))
            rx_n6 = int(tick)
            tx_n3 = int(tick * 0.70 * _jitter(0.01))
        else:
            # Healthy: ~0.1% loss → drop rate ~10 KB/s << 0.5 MB/s threshold
            rx_n3 = int(tick)
            tx_n6 = int(tick * 0.999 * _jitter(0.001))
            rx_n6 = int(tick)
            tx_n3 = int(tick * 0.999 * _jitter(0.001))

        with lock:
            counters[("port_bytes_count",   "rx", "N3")] += rx_n3
            counters[("port_bytes_count",   "tx", "N6")] += tx_n6
            counters[("port_bytes_count",   "rx", "N6")] += rx_n6
            counters[("port_bytes_count",   "tx", "N3")] += tx_n3

            counters[("port_packets_count", "rx", "N3")] += rx_n3 // AVG_PKT_SIZE
            counters[("port_packets_count", "tx", "N6")] += tx_n6 // AVG_PKT_SIZE
            counters[("port_packets_count", "rx", "N6")] += rx_n6 // AVG_PKT_SIZE
            counters[("port_packets_count", "tx", "N3")] += tx_n3 // AVG_PKT_SIZE

            drop_n3 = max(0, rx_n3 - tx_n6) // AVG_PKT_SIZE
            drop_n6 = max(0, rx_n6 - tx_n3) // AVG_PKT_SIZE
            counters[("port_dropped_count", "rx", "N3")] += drop_n3
            counters[("port_dropped_count", "rx", "N6")] += drop_n6

        time.sleep(UPDATE_EVERY)


def render():
    entries = [
        ("port_bytes_count",   "counter", "Bytes received/transmitted by the UPF DPDK port"),
        ("port_packets_count", "counter", "Packets received/transmitted by the UPF DPDK port"),
        ("port_dropped_count", "counter", "Packets dropped on the UPF DPDK port"),
    ]
    lines = []
    with lock:
        for name, mtype, help_text in entries:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {mtype}")
            for (n, d, i), v in counters.items():
                if n == name:
                    lines.append(f'{name}{{dir="{d}",iface="{i}"}} {v}')
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = render().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_):
        pass


threading.Thread(target=simulate, daemon=True).start()
HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
