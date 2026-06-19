// All user-visible strings. Change copy here, never in components.

export const COPY = {
  app: {
    name: "BESS-UPF Intelligence",
    tagline: "5G User Plane Function Monitor",
  },
  nav: {
    overview:  "Overview",
    anomalies: "Anomaly Feed",
    chat:      "Analysis",
    forecast:  "Forecast",
    scenario:  "Scenario",
  },
  overview: {
    kpi: {
      sessions:  "Active Sessions",
      n3Rx:      "N3 Inbound",
      n6Tx:      "N6 Outbound",
      drops:     "Drop Rate",
    },
    events:       "Active Events",
    forecast:     "Trend Forecast",
    systemHealth: "System Health",
    noEvents:     "No active events — all metrics within expected ranges",
    services: {
      prometheus:  "Prometheus",
      vm:          "VictoriaMetrics",
      detection:   "Detection Service",
      llm:         "LLM Backend",
      sim:         "UPF Generator",
    },
  },
  anomalies: {
    title:       "Anomaly Feed",
    empty:       "No active events — all metrics within expected ranges",
    emptyHint:   "Last checked",
    filterLabel: "Filter by severity",
    typeLabel:   "Filter by type",
    colSev:      "",            // icon only
    colType:     "Type",
    colMetric:   "Metric",
    colIface:    "Interface",
    colValue:    "Value",
    colBaseline: "Baseline",
    colTime:     "Time",
    colStatus:   "Status",
    liveBadge:     "LIVE",
    forecastBadge: "FORECAST",
    mlBadge:       "ML",
    aiBadge:       "AI",
  },
  chat: {
    title:         "Analysis",
    placeholder:   "Ask about UPF metrics, anomalies, or capacity… (Enter to send)",
    send:          "Send",
    clearBtn:      "Clear",
    reasoningLbl:  "How I answered this",
    reasoningHide: "Hide",
    reasoningShow: "Show",
    contextActive: (n: number) => n === 0
      ? "No events in context"
      : `${n} event${n === 1 ? "" : "s"} in context`,
    loadingMsg: "Querying VictoriaMetrics and detection service…",
    errorPrefix: "Unable to reach LLM backend — check vLLM service health in the System Health panel.",
    suggestedTitle:  "Suggested questions",
    groups: [
      {
        label: "Current state",
        questions: [
          "What's the current health of the UPF?",
          "Are there any active anomalies right now?",
          "What's the session count trend over the last hour?",
        ],
      },
      {
        label: "Investigating issues",
        questions: [
          "Is the current session count normal for this time of day?",
          "What are the packet drop rates on N3?",
        ],
      },
      {
        label: "Planning ahead",
        questions: [
          "Will we hit capacity in the next 6 hours at current growth?",
          "Which interface is most likely to degrade first?",
        ],
      },
      {
        label: "Temporal intelligence",
        questions: [
          "Is the current traffic regime expected for this time of day?",
          "Which hours today are forecast to be peak load periods?",
          "How does today's session count compare to the historical seasonal baseline?",
          "Are there any upcoming holiday periods that could affect traffic patterns?",
        ],
      },
    ],
  },
  forecast: {
    title: "Trend Forecast",
    etaLabel: "ETA",
    noData: "No forecast data available",
    summary: (metric: string, trendPct: number, etaH: number) =>
      `${metric} is trending ${trendPct >= 0 ? "upward" : "downward"} at ${Math.abs(trendPct).toFixed(1)}% per hour. ` +
      (etaH > 0
        ? `At this rate, configured capacity will be reached in approximately ${etaH.toFixed(1)} hours.`
        : "Current trajectory is within safe operating range."),
  },
  scenario: {
    simBanner: "⚠ SIMULATION MODE — Not connected to production UPF",
    title: "Scenario Control",
    activeSince: (s: number) => {
      const m = Math.floor(s / 60), sec = s % 60;
      return `Running for ${m}m ${sec}s`;
    },
    descriptions: {
      normal:             "Steady-state baseline — nominal session load with diurnal variation",
      session_spike:      "Sudden surge in PFCP sessions (4× baseline) — triggers z-score on pfcp_sessions_total",
      session_drop:       "Reduced session load — useful for baseline calibration and recovery testing",
      packet_drop_surge:  "Elevated drop rate on N3 interface — triggers z-score on port_dropped_count",
      asymmetric_traffic: "Skewed N3/N6 traffic ratio — high inbound, reduced outbound throughput",
      flatline:           "All counters drop to zero — simulates complete link or UPF failure",
    } as Record<string, string>,
    toastStart: (name: string) => `Scenario '${name}' started — auto-reverts in 10 minutes.`,
    toastStop:  "Scenario stopped — returning to normal mode.",
  },
  contextPanel: {
    defaultTitle: "Context",
    defaultBody:  "Select an event or start a conversation to see details here.",
    askAI:        "Ask AI about this",
    viewChart:    "View in Forecast",
    queries:      "PromQL Queries Used",
    noQueries:    "No queries were executed",
    events:       "Referenced Events",
    noEvents:     "No events referenced",
  },
  onboarding: {
    title: "Welcome to BESS-UPF Intelligence",
    body: "This is a three-panel interface: the sidebar shows live system pulse and navigation; the main area shows the current view; the right panel shows context for whatever you've selected. Start with the Overview for a management summary, or jump to Analysis to ask questions in plain English.",
    cta: "Got it",
  },
};
