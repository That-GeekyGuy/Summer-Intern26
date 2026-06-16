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

// Capacity ceilings — used by KPI cards and forecast view
// To change these: update this map. No component code needs to change.
export const CAPACITY_THRESHOLDS: Record<string, number> = {
  pfcp_sessions_total: 20000,        // max 20k PFCP sessions
  "port_bytes_count_N3_rx": 1e9,     // 1 Gbps N3 inbound
  "port_bytes_count_N6_tx": 1e9,     // 1 Gbps N6 outbound
  port_dropped_count: 1000,          // 1000 drops/s = warning
};
