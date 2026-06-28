import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Credentials, fetchBenchmark, TierResult } from "../../api/client";

interface Props { creds: Credentials; }

function f1Color(f1?: number): string {
  if (f1 == null) return "var(--text-muted)";
  if (f1 >= 0.8) return "var(--signal-ok)";
  if (f1 >= 0.6) return "var(--signal-warning)";
  return "var(--signal-critical)";
}

function pct(v?: number) { return v != null ? `${(v * 100).toFixed(1)}%` : "—"; }
function ms(v?: number)  { return v != null ? `${v.toFixed(2)} ms` : "—"; }
function num(v?: number) { return v != null ? String(v) : "—"; }

function TierRow({ tier }: { tier: TierResult }) {
  const [expanded, setExpanded] = useState(false);
  const hasCounts = tier.tp != null || tier.fp != null;

  return (
    <>
      <tr
        onClick={() => hasCounts && setExpanded(e => !e)}
        style={{
          cursor: hasCounts ? "pointer" : "default",
          opacity: tier.available ? 1 : 0.45,
          borderBottom: "1px solid var(--border)",
        }}
      >
        <td style={{ padding: "9px 14px", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>
          {tier.id}
        </td>
        <td style={{ padding: "9px 14px", fontSize: "var(--text-xs)" }}>
          {tier.label}
          {!tier.available && (
            <span style={{ marginLeft: 8, fontSize: "var(--text-2xs)", color: "var(--text-muted)", fontStyle: "italic" }}>
              {tier.note ?? "unavailable"}
            </span>
          )}
        </td>
        <td style={{ padding: "9px 14px", textAlign: "right", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)" }}>
          {tier.available ? pct(tier.precision) : "—"}
        </td>
        <td style={{ padding: "9px 14px", textAlign: "right", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)" }}>
          {tier.available ? pct(tier.recall) : "—"}
        </td>
        <td style={{ padding: "9px 14px", textAlign: "right", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", fontWeight: 700, color: f1Color(tier.available ? tier.f1 : undefined) }}>
          {tier.available ? pct(tier.f1) : "—"}
        </td>
        <td style={{ padding: "9px 14px", textAlign: "right", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)" }}>
          {tier.available && tier.auc_roc != null ? tier.auc_roc.toFixed(3) : "—"}
        </td>
        <td style={{ padding: "9px 14px", textAlign: "right", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
          {tier.available ? ms(tier.latency_p50_ms) : "—"}
        </td>
        <td style={{ padding: "9px 14px", textAlign: "center", fontSize: 10, color: "var(--text-muted)", width: 24 }}>
          {hasCounts ? (expanded ? "▲" : "▼") : ""}
        </td>
      </tr>
      {expanded && hasCounts && (
        <tr style={{ background: "var(--bg-elevated)" }}>
          <td colSpan={8} style={{ padding: "8px 28px 12px" }}>
            <div style={{ display: "flex", gap: 24, fontSize: "var(--text-2xs)", fontFamily: "var(--font-mono)", color: "var(--text-secondary)" }}>
              <span>TP: <strong style={{ color: "var(--signal-ok)" }}>{num(tier.tp)}</strong></span>
              <span>FP: <strong style={{ color: "var(--signal-warning)" }}>{num(tier.fp)}</strong></span>
              <span>FN: <strong style={{ color: "var(--signal-critical)" }}>{num(tier.fn)}</strong></span>
              <span>TN: <strong>{num(tier.tn)}</strong></span>
            </div>
            {tier.note && (
              <div style={{ marginTop: 4, fontSize: "var(--text-2xs)", color: "var(--text-muted)", fontStyle: "italic" }}>
                {tier.note}
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

export function BenchmarkPage({ creds }: Props) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["benchmark"],
    queryFn: () => fetchBenchmark(creds),
    staleTime: 5 * 60_000,
    retry: 1,
  });

  if (isLoading) {
    return <div style={{ padding: 32, color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>Loading benchmark report…</div>;
  }
  if (error || !data) {
    return <div style={{ padding: 32, color: "var(--signal-critical)", fontSize: "var(--text-sm)" }}>Failed to load benchmark report.</div>;
  }
  if (!data.available) {
    return (
      <div style={{ padding: 32, display: "flex", flexDirection: "column", gap: 12 }}>
        <h2 style={{ margin: 0, fontSize: "var(--text-lg)", fontWeight: 600 }}>Tier Ablation Benchmark</h2>
        <div style={{ background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: 6, padding: "24px 28px", maxWidth: 520 }}>
          <div style={{ fontSize: "1.4rem", marginBottom: 8 }}>◉</div>
          <p style={{ margin: 0, color: "var(--text-muted)", lineHeight: 1.6, fontSize: "var(--text-sm)" }}>No ablation report found.</p>
          <p style={{ margin: "8px 0 0", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>
            {data.message ?? "Run tools/train/ablation.py then restart the analysis service."}
          </p>
        </div>
      </div>
    );
  }

  const tiers    = data.tiers ?? [];
  const ensemble = data.ensemble;

  return (
    <div style={{ padding: "20px 24px", overflow: "auto", height: "100%" }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 16, marginBottom: 20 }}>
        <h2 style={{ margin: 0, fontFamily: "var(--font-mono)", fontSize: "var(--text-lg)", fontWeight: 700 }}>
          Tier Ablation Benchmark
        </h2>
        <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
          T1 → T2a → T2b · zero-shot · fine-tuned · ensemble
        </span>
      </div>

      <div style={{ display: "flex", gap: 24, marginBottom: 16, fontSize: "var(--text-2xs)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
        {data.generated_at && <span>Generated: {new Date(data.generated_at).toLocaleString()}</span>}
        {data.dataset_hash && <span>Dataset hash: {data.dataset_hash}</span>}
      </div>

      <div style={{ background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: 6, overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ background: "var(--bg-elevated)", borderBottom: "1px solid var(--border)" }}>
              {["Tier", "Method", "Precision", "Recall", "F1", "AUC-ROC", "Latency P50", ""].map(h => (
                <th key={h} style={{
                  padding: "8px 14px",
                  textAlign: (h === "Tier" || h === "Method" || h === "") ? "left" : "right",
                  fontSize: "var(--text-2xs)", color: "var(--text-muted)", fontWeight: 600,
                  textTransform: "uppercase", letterSpacing: "0.05em", fontFamily: "var(--font-mono)",
                }}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {tiers.map(t => <TierRow key={t.id} tier={t} />)}
            {ensemble?.available && (
              <tr style={{ background: "rgba(124,58,237,0.05)", borderTop: "2px solid var(--border)", fontWeight: 700 }}>
                <td style={{ padding: "9px 14px", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", color: "#a78bfa" }}>{ensemble.id}</td>
                <td style={{ padding: "9px 14px", fontSize: "var(--text-xs)", color: "#a78bfa" }}>{ensemble.label}</td>
                <td style={{ padding: "9px 14px", textAlign: "right", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)" }}>{pct(ensemble.precision)}</td>
                <td style={{ padding: "9px 14px", textAlign: "right", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)" }}>{pct(ensemble.recall)}</td>
                <td style={{ padding: "9px 14px", textAlign: "right", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", color: f1Color(ensemble.f1) }}>{pct(ensemble.f1)}</td>
                <td colSpan={3} style={{ padding: "9px 14px", fontSize: "var(--text-2xs)", color: "var(--text-muted)", fontStyle: "italic" }}>
                  {ensemble.note}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div style={{ marginTop: 16, fontSize: "var(--text-2xs)", color: "var(--text-muted)", lineHeight: 1.7, maxWidth: 680 }}>
        <strong>T2b-zs</strong> (MOMENT Zero-Shot) evaluated on all windows — no train contamination (zero-shot uses no learned params).{" "}
        <strong>T2a</strong> / <strong>T2b-ft</strong> evaluated on stratified eval split (P1.1 episode-aware split).{" "}
        Click a tier row to expand TP / FP / FN / TN counts.{" "}
        Latency is pure Python on CPU; Go production path is ~10× faster.
      </div>
    </div>
  );
}
