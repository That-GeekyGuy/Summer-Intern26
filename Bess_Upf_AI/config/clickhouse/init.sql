-- config/clickhouse/init.sql
CREATE DATABASE IF NOT EXISTS bess_upf;

USE bess_upf;

------------------------------------------------------------------------
-- upf_metrics
------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS upf_metrics_kafka (
    upf_id                     String,
    ts                         Float64,
    pfcp_sessions_total        UInt64,
    port_bytes_N3_rx           UInt64,   port_bytes_N3_rx_rate   Float64,
    port_bytes_N6_tx           UInt64,   port_bytes_N6_tx_rate   Float64,
    port_pkts_N3_rx            UInt64,   port_pkts_N3_rx_rate    Float64,
    port_pkts_N6_tx            UInt64,   port_pkts_N6_tx_rate    Float64,
    port_dropped_N3_rx         UInt64,   port_dropped_N3_rx_rate Float64,
    port_dropped_N6_rx         UInt64,   port_dropped_N6_rx_rate Float64,
    upf_sim_scenario_normal    UInt8,    upf_sim_scenario_congestion UInt8,
    upf_sim_scenario_flatline  UInt8,    upf_sim_scenario_spike  UInt8,
    go_goroutines              UInt64,   go_heap_alloc_bytes     UInt64,
    process_cpu_seconds_total  Float64,  process_cpu_rate        Float64
) ENGINE = Kafka
SETTINGS
    kafka_broker_list          = 'redpanda:9092',
    kafka_topic_list           = 'upf.metrics.raw',
    kafka_group_name           = 'clickhouse-metrics',
    kafka_format               = 'JSONEachRow',
    kafka_skip_broken_messages = 1;

CREATE TABLE IF NOT EXISTS upf_metrics (
    upf_id                     LowCardinality(String),
    ts                         DateTime64(3),
    pfcp_sessions_total        UInt64,
    port_bytes_N3_rx           UInt64,   port_bytes_N3_rx_rate   Float64,
    port_bytes_N6_tx           UInt64,   port_bytes_N6_tx_rate   Float64,
    port_pkts_N3_rx            UInt64,   port_pkts_N3_rx_rate    Float64,
    port_pkts_N6_tx            UInt64,   port_pkts_N6_tx_rate    Float64,
    port_dropped_N3_rx         UInt64,   port_dropped_N3_rx_rate Float64,
    port_dropped_N6_rx         UInt64,   port_dropped_N6_rx_rate Float64,
    upf_sim_scenario_normal    UInt8,    upf_sim_scenario_congestion UInt8,
    upf_sim_scenario_flatline  UInt8,    upf_sim_scenario_spike  UInt8,
    go_goroutines              UInt64,   go_heap_alloc_bytes     UInt64,
    process_cpu_seconds_total  Float64,  process_cpu_rate        Float64
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (upf_id, ts)
TTL toDateTime(ts) + INTERVAL 90 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS upf_metrics_mv TO upf_metrics AS
SELECT
    upf_id,
    toDateTime64(ts, 3) AS ts,
    pfcp_sessions_total,
    port_bytes_N3_rx,    port_bytes_N3_rx_rate,
    port_bytes_N6_tx,    port_bytes_N6_tx_rate,
    port_pkts_N3_rx,     port_pkts_N3_rx_rate,
    port_pkts_N6_tx,     port_pkts_N6_tx_rate,
    port_dropped_N3_rx,  port_dropped_N3_rx_rate,
    port_dropped_N6_rx,  port_dropped_N6_rx_rate,
    upf_sim_scenario_normal, upf_sim_scenario_congestion,
    upf_sim_scenario_flatline, upf_sim_scenario_spike,
    go_goroutines,       go_heap_alloc_bytes,
    process_cpu_seconds_total, process_cpu_rate
FROM upf_metrics_kafka;

------------------------------------------------------------------------
-- anomaly_events
------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS anomaly_events_kafka (
    upf_id                  String,
    ts                      Float64,
    anomaly                 UInt8,
    anomaly_score           Float64,
    threshold               Float64,
    top_anomalous_channels  Array(String),
    model_version           String,
    window_end_offset       Int64
) ENGINE = Kafka
SETTINGS
    kafka_broker_list          = 'redpanda:9092',
    kafka_topic_list           = 'upf.anomalies.critical',
    kafka_group_name           = 'clickhouse-anomaly',
    kafka_format               = 'JSONEachRow',
    kafka_skip_broken_messages = 1;

CREATE TABLE IF NOT EXISTS anomaly_events (
    upf_id                  LowCardinality(String),
    ts                      DateTime64(3),
    anomaly                 UInt8,
    anomaly_score           Float64,
    threshold               Float64,
    top_anomalous_channels  Array(String),
    model_version           String,
    window_end_offset       Int64
) ENGINE = ReplacingMergeTree(ts)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (upf_id, ts)
TTL toDateTime(ts) + INTERVAL 90 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS anomaly_events_mv TO anomaly_events AS
SELECT
    upf_id,
    toDateTime64(ts, 3) AS ts,
    anomaly, anomaly_score, threshold,
    top_anomalous_channels, model_version, window_end_offset
FROM anomaly_events_kafka;

-- ============================================================
-- Shadow detection log: compare v2 scores vs v1 offline
-- ============================================================

CREATE TABLE IF NOT EXISTS shadow_detections_kafka (
    upf_id        String,
    ts            Float64,
    anomaly       UInt8,
    anomaly_score Float64,
    if_score      Float64,
    rf_proba      Float64,
    moment_score  Float64,
    model_version String
) ENGINE = Kafka
SETTINGS
    kafka_broker_list        = 'redpanda:9092',
    kafka_topic_list         = 'upf.shadow.detections',
    kafka_group_name         = 'clickhouse-shadow',
    kafka_format             = 'JSONEachRow',
    kafka_skip_broken_messages = 1;

CREATE TABLE IF NOT EXISTS shadow_detections (
    upf_id        LowCardinality(String),
    ts            DateTime64(3),
    anomaly       UInt8,
    anomaly_score Float64,
    if_score      Float64,
    rf_proba      Float64,
    moment_score  Float64,
    model_version LowCardinality(String)
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (upf_id, ts)
TTL toDateTime(ts) + INTERVAL 90 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS shadow_detections_mv
TO shadow_detections AS
SELECT
    upf_id,
    toDateTime64(ts, 3) AS ts,
    anomaly,
    anomaly_score,
    if_score,
    rf_proba,
    moment_score,
    model_version
FROM shadow_detections_kafka;
