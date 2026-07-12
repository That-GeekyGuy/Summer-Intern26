-- charts/bess-upf/charts/clickhouse/files/init.sql
-- k8s variant of config/clickhouse/init.sql: MergeTree -> ReplicatedMergeTree
-- so each of the 3 StatefulSet replicas holds a full copy, synced via Keeper.
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
    kafka_skip_broken_messages = 1,
    kafka_flush_interval_ms   = 1000;

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
) ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/upf_metrics', '{replica}')
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

-- The materialized view below reads from anomaly_events_kafka — drop it
-- first so ClickHouse doesn't refuse to drop/recreate that table as "in use".
DROP VIEW IF EXISTS anomaly_events_mv;

-- Kafka-engine tables hold no data themselves (pure streaming cursor) and
-- ClickHouse categorically rejects "ALTER ... ADD COLUMN" on them
-- (NOT_IMPLEMENTED). Drop+recreate is the only way to add columns — safe
-- here since Kafka consumer offsets are tracked server-side by the
-- kafka_group_name, not by this table object's lifecycle.
DROP TABLE IF EXISTS anomaly_events_kafka;
CREATE TABLE anomaly_events_kafka (
    upf_id                  String,
    ts                      Float64,
    anomaly                 UInt8,
    anomaly_score           Float64,
    threshold               Float64,
    top_anomalous_channels  Array(String),
    model_version           String,
    window_end_offset       Int64,
    -- Populated only for event_type="predictive" rows (forecast-breach
    -- warnings); NULL for reactive/ml rows. See ClassifyAnomaly in
    -- analysis/internal/chclient/client.go.
    predicted_crossing_time Nullable(Float64),
    forecast_horizon        Nullable(String)
) ENGINE = Kafka
SETTINGS
    kafka_broker_list          = 'redpanda:9092',
    kafka_topic_list           = 'upf.anomalies.critical',
    kafka_group_name           = 'clickhouse-anomaly',
    kafka_format               = 'JSONEachRow',
    kafka_skip_broken_messages = 1,
    kafka_flush_interval_ms   = 1000;

CREATE TABLE IF NOT EXISTS anomaly_events (
    upf_id                  LowCardinality(String),
    ts                      DateTime64(3),
    anomaly                 UInt8,
    anomaly_score           Float64,
    threshold               Float64,
    top_anomalous_channels  Array(String),
    model_version           String,
    window_end_offset       Int64,
    predicted_crossing_time Nullable(Float64),
    forecast_horizon        Nullable(String)
) ENGINE = ReplicatedReplacingMergeTree('/clickhouse/tables/{shard}/anomaly_events', '{replica}', ts)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (upf_id, ts)
TTL toDateTime(ts) + INTERVAL 90 DAY;

-- Upgrade path for clusters where anomaly_events already existed before the
-- predictive-tier columns were added (CREATE TABLE IF NOT EXISTS above is a
-- no-op against an already-existing table). Unlike the Kafka table above,
-- ReplicatedReplacingMergeTree supports ALTER ADD COLUMN natively.
ALTER TABLE anomaly_events ADD COLUMN IF NOT EXISTS predicted_crossing_time Nullable(Float64);
ALTER TABLE anomaly_events ADD COLUMN IF NOT EXISTS forecast_horizon Nullable(String);

CREATE MATERIALIZED VIEW anomaly_events_mv TO anomaly_events AS
SELECT
    upf_id,
    toDateTime64(ts, 3) AS ts,
    anomaly, anomaly_score, threshold,
    top_anomalous_channels, model_version, window_end_offset,
    predicted_crossing_time, forecast_horizon
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
    kafka_skip_broken_messages = 1,
    kafka_flush_interval_ms = 1000;

CREATE TABLE IF NOT EXISTS shadow_detections (
    upf_id        LowCardinality(String),
    ts            DateTime64(3),
    anomaly       UInt8,
    anomaly_score Float64,
    if_score      Float64,
    rf_proba      Float64,
    moment_score  Float64,
    model_version LowCardinality(String)
) ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/shadow_detections', '{replica}')
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

-- Phase 3: action audit
CREATE TABLE IF NOT EXISTS action_audit_kafka (
    action_id      String,
    upf_id         String,
    ts             Float64,
    action_class   String,
    trust_level    String,
    dry_run        UInt8,
    success        UInt8,
    message        String,
    anomaly_score  Float64,
    model_version  String,
    rollback_token String
) ENGINE = Kafka SETTINGS
    kafka_broker_list = 'redpanda:9092',
    kafka_topic_list  = 'upf.action_audit',
    kafka_group_name  = 'clickhouse-audit',
    kafka_format      = 'JSONEachRow',
    kafka_skip_broken_messages = 1,
    kafka_flush_interval_ms = 1000;

CREATE TABLE IF NOT EXISTS action_audit (
    action_id      String,
    upf_id         LowCardinality(String),
    ts             DateTime64(3),
    action_class   LowCardinality(String),
    trust_level    LowCardinality(String),
    dry_run        UInt8,
    success        UInt8,
    message        String,
    anomaly_score  Float64,
    model_version  LowCardinality(String),
    rollback_token String
) ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/action_audit', '{replica}')
PARTITION BY toYYYYMMDD(ts)
ORDER BY (upf_id, ts)
TTL toDateTime(ts) + INTERVAL 365 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS action_audit_mv TO action_audit AS
SELECT
    action_id,
    upf_id,
    toDateTime64(ts, 3) AS ts,
    action_class,
    trust_level,
    dry_run,
    success,
    message,
    anomaly_score,
    model_version,
    rollback_token
FROM action_audit_kafka;

-- ============================================================
-- Operator Feedback Loop
-- ============================================================

CREATE TABLE IF NOT EXISTS operator_feedback_kafka (
    action_id      String,
    upf_id         String,
    label          String,
    ts             Float64
) ENGINE = Kafka SETTINGS
    kafka_broker_list = 'redpanda:9092',
    kafka_topic_list  = 'upf.feedback',
    kafka_group_name  = 'clickhouse-feedback',
    kafka_format      = 'JSONEachRow',
    kafka_skip_broken_messages = 1,
    kafka_flush_interval_ms = 1000;

CREATE TABLE IF NOT EXISTS operator_feedback (
    action_id      String,
    upf_id         LowCardinality(String),
    label          LowCardinality(String),
    ts             DateTime64(3)
) ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/operator_feedback', '{replica}')
PARTITION BY toYYYYMMDD(ts)
ORDER BY (upf_id, ts)
TTL toDateTime(ts) + INTERVAL 365 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS operator_feedback_mv TO operator_feedback AS
SELECT
    action_id,
    upf_id,
    label,
    toDateTime64(ts, 3) AS ts
FROM operator_feedback_kafka;
