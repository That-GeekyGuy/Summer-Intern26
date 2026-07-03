# pipeline/tests/test_scraper_schema.py
"""
Unit tests for parse_prometheus_text() and compute_rate() in scrape_to_kafka.py.
Run from repo root: PYTHONPATH=. pytest pipeline/tests/test_scraper_schema.py -v
"""
import pytest

SAMPLE_PROM_TEXT = """
# HELP pfcp_sessions_total Total PFCP sessions
# TYPE pfcp_sessions_total counter
pfcp_sessions_total{node_id="sim-upf-01"} 730141

# HELP port_bytes_count Total port bytes
# TYPE port_bytes_count counter
port_bytes_count{dir="rx",iface="N3",node_id="sim-upf-01"} 1234567890
port_bytes_count{dir="tx",iface="N6",node_id="sim-upf-01"} 987654321

# HELP port_packets_count Total port packets
# TYPE port_packets_count counter
port_packets_count{dir="rx",iface="N3",node_id="sim-upf-01"} 8901234
port_packets_count{dir="tx",iface="N6",node_id="sim-upf-01"} 7812345

# HELP port_dropped_count Dropped packets
# TYPE port_dropped_count counter
port_dropped_count{dir="rx",iface="N3",node_id="sim-upf-01"} 4200
port_dropped_count{dir="rx",iface="N6",node_id="sim-upf-01"} 12

# HELP upf_sim_scenario Current scenario
# TYPE upf_sim_scenario gauge
upf_sim_scenario{mode="normal"} 1
upf_sim_scenario{mode="congestion"} 0
upf_sim_scenario{mode="flatline"} 0
upf_sim_scenario{mode="spike"} 0

# HELP go_goroutines Number of goroutines
# TYPE go_goroutines gauge
go_goroutines 42

# HELP go_heap_alloc_bytes Heap bytes allocated
# TYPE go_heap_alloc_bytes gauge
go_heap_alloc_bytes 2097152

# HELP process_cpu_seconds_total CPU seconds used
# TYPE process_cpu_seconds_total counter
process_cpu_seconds_total 1.234
"""


def test_parse_returns_upf_id():
    from pipeline.scrape_to_kafka import parse_prometheus_text
    _, upf_id = parse_prometheus_text(SAMPLE_PROM_TEXT)
    assert upf_id == "sim-upf-01"


def test_parse_port_bytes_rx():
    from pipeline.scrape_to_kafka import parse_prometheus_text
    result, _ = parse_prometheus_text(SAMPLE_PROM_TEXT)
    assert "port_bytes_N3_rx" in result
    assert result["port_bytes_N3_rx"] == 1234567890.0


def test_parse_port_bytes_tx():
    from pipeline.scrape_to_kafka import parse_prometheus_text
    result, _ = parse_prometheus_text(SAMPLE_PROM_TEXT)
    assert "port_bytes_N6_tx" in result
    assert result["port_bytes_N6_tx"] == 987654321.0


def test_parse_port_pkts():
    from pipeline.scrape_to_kafka import parse_prometheus_text
    result, _ = parse_prometheus_text(SAMPLE_PROM_TEXT)
    assert result["port_pkts_N3_rx"] == 8901234.0
    assert result["port_pkts_N6_tx"] == 7812345.0


def test_parse_port_dropped():
    from pipeline.scrape_to_kafka import parse_prometheus_text
    result, _ = parse_prometheus_text(SAMPLE_PROM_TEXT)
    assert "port_dropped_N3_rx" in result
    assert result["port_dropped_N6_rx"] == 12.0


def test_parse_scenario_keys():
    from pipeline.scrape_to_kafka import parse_prometheus_text
    result, _ = parse_prometheus_text(SAMPLE_PROM_TEXT)
    assert result["upf_sim_scenario_normal"] == 1.0
    assert result["upf_sim_scenario_congestion"] == 0.0
    assert "upf_sim_scenario_flatline" in result
    assert "upf_sim_scenario_spike" in result


def test_parse_gauge_keys():
    from pipeline.scrape_to_kafka import parse_prometheus_text
    result, _ = parse_prometheus_text(SAMPLE_PROM_TEXT)
    assert result["go_goroutines"] == 42.0
    assert result["go_heap_alloc_bytes"] == 2097152.0


def test_parse_pfcp():
    from pipeline.scrape_to_kafka import parse_prometheus_text
    result, _ = parse_prometheus_text(SAMPLE_PROM_TEXT)
    assert "pfcp_sessions_total" in result
    assert result["pfcp_sessions_total"] == 730141.0


def test_no_rate_keys_from_parse():
    """parse_prometheus_text must NOT produce _rate keys — that is compute_rate's job."""
    from pipeline.scrape_to_kafka import parse_prometheus_text
    result, _ = parse_prometheus_text(SAMPLE_PROM_TEXT)
    assert "port_bytes_N3_rx_rate" not in result
    assert "process_cpu_rate" not in result


def test_rate_none_on_first_tick():
    from pipeline.scrape_to_kafka import compute_rate
    prev: dict = {}
    assert compute_rate("pfcp_sessions_total", 730141.0, 1000.0, prev) is None


def test_rate_computed_on_second_tick():
    from pipeline.scrape_to_kafka import compute_rate
    prev: dict = {}
    compute_rate("pfcp_sessions_total", 730000.0, 1000.0, prev)
    rate = compute_rate("pfcp_sessions_total", 730060.0, 1001.0, prev)
    assert rate == pytest.approx(60.0)


def test_counter_reset_returns_none():
    from pipeline.scrape_to_kafka import compute_rate
    prev: dict = {}
    compute_rate("pfcp_sessions_total", 730141.0, 1000.0, prev)
    # value decreased = counter reset (process restarted)
    assert compute_rate("pfcp_sessions_total", 100.0, 1001.0, prev) is None
