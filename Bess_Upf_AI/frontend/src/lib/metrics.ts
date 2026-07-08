import { fmtBytes, fmtDrops, fmtSessions } from "./formatters";

// Format a numeric value using the appropriate unit for a given metric name.
// Predictive events store values in rate units (bytes/s, drops/s, sessions).
// ML multivariate events store a composite anomaly score — show as raw score.
export function fmtMetricVal(metricName: string, value: number): string {
  if (isNaN(value)) return "—";
  // Composite/AI score metrics — not a physical unit
  if (metricName.includes("multivariate") || metricName.includes("ai")) {
    return Math.abs(value) < 0.001 ? "—" : value.toFixed(3);
  }
  if (metricName.includes("bytes")) return fmtBytes(value);
  if (metricName.includes("dropped")) return fmtDrops(value);
  return fmtSessions(value);
}

// Human-readable display names for raw metric identifiers.
// Engineers see the raw name only in the reasoning trail.
// Everyone else sees these display names.

export const metricDisplayNames: Record<string, string> = {
  pfcp_sessions_total: "Active Sessions",
  upf_pdu_sessions_total: "PDU Sessions",
  "port_bytes_count{dir=\"rx\",iface=\"N3\"}": "N3 Inbound Throughput",
  "port_bytes_count{dir=\"tx\",iface=\"N3\"}": "N3 Outbound Throughput",
  "port_bytes_count{dir=\"rx\",iface=\"N6\"}": "N6 Inbound Throughput",
  "port_bytes_count{dir=\"tx\",iface=\"N6\"}": "N6 Outbound Throughput",
  port_bytes_count: "Interface Throughput",
  port_dropped_count: "Packet Drops",
  port_packets_count: "Packet Count",
  upf_sim_scenario: "Simulation Scenario",
  detection_poll_errors_total: "Detection Poll Errors",
  detection_sqlite_stored_events: "Stored Anomaly Events",
  analysis_active_sessions: "Active Chat Sessions",
  // Tier 2 AI synthetic metric names (emitted by AIEvaluator)
  upf_multivariate_ai: "UPF Multivariate (AI)",
  upf_multivariate: "UPF Multivariate",
  // MOMENT channel names — used in channel attribution bars
  port_bytes_N3_rx_rate:    "N3 Inbound Throughput",
  port_bytes_N6_tx_rate:    "N6 Outbound Throughput",
  port_pkts_N3_rx_rate:     "N3 Packet Rate",
  port_dropped_N3_rx_rate:  "N3 Drop Rate",
  port_dropped_N6_rx_rate:  "N6 Drop Rate",
  pfcp_session_setup_rate:  "Session Setup Rate",
  dl_forwarding_efficiency: "DL Forwarding Eff.",
  dl_throughput_efficiency: "DL Throughput Eff.",
  drop_rate_percentage:     "Drop Rate (%)",
  tsi_value:                "TSI Value",
  uoi_value:                "UPF Overload Index",
  uoi_session_component:    "UOI Session",
  uoi_throughput_component: "UOI Throughput",
  go_goroutines:            "Go Goroutines",
  go_heap_alloc_bytes:      "Heap Alloc",
  gc_pressure_rate:         "GC Pressure Rate",
};

export function displayName(metric: string): string {
  // Try exact match first
  if (metricDisplayNames[metric]) return metricDisplayNames[metric];
  // Try prefix match (strip labels)
  const base = metric.split("{")[0];
  if (metricDisplayNames[base]) return metricDisplayNames[base];
  // Fall back to prettifying the raw name
  return base
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .replace(/\bUpf\b/, "UPF")
    .replace(/\bPfcp\b/, "PFCP")
    .replace(/\bN3\b|\bN6\b/, (m) => m.toUpperCase());
}

// Parse label JSON string and format for display (e.g. "N3 rx")
export function formatLabels(labelsJson: string): string {
  try {
    const obj = JSON.parse(labelsJson) as Record<string, string>;
    const parts: string[] = [];
    if (obj.iface) parts.push(obj.iface);
    if (obj.dir) parts.push(obj.dir);
    if (obj.node_id) parts.push(obj.node_id);
    if (obj.job && obj.job !== "upf") parts.push(obj.job);
    return parts.join(" ") || "all";
  } catch {
    return labelsJson || "all";
  }
}

// Severity → signal color
export function severityColor(severity: string): string {
  switch (severity.toLowerCase()) {
    case "critical": return "var(--signal-critical)";
    case "high":     return "var(--signal-warning)";
    case "warning":  return "var(--signal-warning)";
    case "medium":   return "var(--signal-warning)";
    case "low":      return "var(--signal-neutral)";
    default:         return "var(--signal-neutral)";
  }
}

// Capacity ceilings — used by KPI cards and forecast view.
// Must stay in sync with the forecast.targets[*].capacity values in rules.yml.
export const CAPACITY_THRESHOLDS: Record<string, number> = {
  pfcp_sessions_total: 2_000_000,    // 20 lakhs per node (sim base 10 lakhs, peak 13 lakhs)
  "port_bytes_count_N3_rx": 10e9,    // 10 GB/s N3 inbound (80 Gbps link)
  "port_bytes_count_N6_tx": 10e9,    // 10 GB/s N6 outbound
  port_dropped_count: 1000,          // 1000 drops/s = warning
};
