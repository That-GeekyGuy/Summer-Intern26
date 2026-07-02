"""
Integration test for ClickHouse schema correctness.
Requires ClickHouse: docker compose -f docker-compose.v2.yml up -d clickhouse

Run: PYTHONPATH=. pytest pipeline/tests/test_clickhouse.py -v
"""
import datetime
import time
import pytest

try:
    import clickhouse_connect
    CH_AVAILABLE = True
except ImportError:
    CH_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not CH_AVAILABLE, reason="clickhouse-connect not installed"
)


@pytest.fixture(scope="module")
def ch():
    try:
        client = clickhouse_connect.get_client(
            host="localhost",
            port=8123,
            username="chuser",
            password="localdev123",
            database="bess_upf",
        )
        client.ping()
        return client
    except Exception as exc:
        pytest.skip(f"ClickHouse not reachable at localhost:8123: {exc}")


def test_tables_exist(ch):
    result = ch.query("SHOW TABLES IN bess_upf")
    tables = {row[0] for row in result.result_rows}
    assert "upf_metrics" in tables
    assert "anomaly_events" in tables
    assert "upf_metrics_kafka" in tables


def test_insert_roundtrip(ch):
    ts_before = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)

    ch.insert(
        "upf_metrics",
        [[
            "sim-upf-test", ts_before,
            730141,          # pfcp_sessions_total UInt64
            1234567890, 12440.0,   # port_bytes_N3_rx UInt64, rate Float64
            987654321,  9876.2,    # port_bytes_N6_tx UInt64, rate Float64
            8901234,    890.1,     # port_pkts_N3_rx UInt64, rate Float64
            7812345,    781.2,     # port_pkts_N6_tx UInt64, rate Float64
            4200,       0.0,       # port_dropped_N3_rx UInt64, rate Float64
            12,         0.0,       # port_dropped_N6_rx UInt64, rate Float64
            1, 0, 0, 0,            # upf_sim_scenario_* UInt8
            42,         2097152,   # go_goroutines, go_heap_alloc_bytes UInt64
            1.234,      0.02,      # process_cpu_seconds_total, process_cpu_rate Float64
        ]],
        column_names=[
            "upf_id", "ts",
            "pfcp_sessions_total",
            "port_bytes_N3_rx", "port_bytes_N3_rx_rate",
            "port_bytes_N6_tx", "port_bytes_N6_tx_rate",
            "port_pkts_N3_rx", "port_pkts_N3_rx_rate",
            "port_pkts_N6_tx", "port_pkts_N6_tx_rate",
            "port_dropped_N3_rx", "port_dropped_N3_rx_rate",
            "port_dropped_N6_rx", "port_dropped_N6_rx_rate",
            "upf_sim_scenario_normal", "upf_sim_scenario_congestion",
            "upf_sim_scenario_flatline", "upf_sim_scenario_spike",
            "go_goroutines", "go_heap_alloc_bytes",
            "process_cpu_seconds_total", "process_cpu_rate",
        ],
    )

    time.sleep(1)

    result = ch.query(
        "SELECT upf_id, pfcp_sessions_total, ts "
        "FROM upf_metrics WHERE upf_id = 'sim-upf-test' "
        "ORDER BY ts DESC LIMIT 1"
    )
    assert result.row_count == 1
    row = result.first_row
    assert row[0] == "sim-upf-test"
    assert row[1] == 730141

    stored_ts: datetime.datetime = row[2]
    # stored_ts may be naive or UTC-aware depending on clickhouse-connect version
    if stored_ts.tzinfo is None:
        stored_ts = stored_ts.replace(tzinfo=datetime.timezone.utc)
    diff = abs((stored_ts - ts_before).total_seconds())
    assert diff < 1.0, f"Timestamp roundtrip drift: {diff}s"


def test_no_pfcp_zero_rows(ch):
    """Guard: no row should have pfcp_sessions_total = 0 (silent zero indicator)."""
    result = ch.query(
        "SELECT count(*) FROM upf_metrics WHERE pfcp_sessions_total = 0"
    )
    count = result.first_row[0]
    assert count == 0, (
        f"{count} rows with pfcp_sessions_total=0 — "
        "old stub pipeline may have written silent zeros to the topic"
    )
