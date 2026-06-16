// All number/time formatting. No raw numbers in components.

export function fmtSessions(n: number): string {
  if (isNaN(n)) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000)     return `${(n / 1_000).toFixed(1)}k`;
  return n.toFixed(0);
}

export function fmtBytes(bytesPerSec: number): string {
  if (isNaN(bytesPerSec)) return "—";
  if (bytesPerSec >= 1e9) return `${(bytesPerSec / 1e9).toFixed(2)} Gbps`;
  if (bytesPerSec >= 1e6) return `${(bytesPerSec / 1e6).toFixed(1)} Mbps`;
  if (bytesPerSec >= 1e3) return `${(bytesPerSec / 1e3).toFixed(1)} Kbps`;
  return `${bytesPerSec.toFixed(0)} bps`;
}

export function fmtDrops(dropsPerSec: number): string {
  if (isNaN(dropsPerSec)) return "—";
  return `${dropsPerSec.toFixed(1)} drops/s`;
}

export function fmtTimestamp(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC");
}

export function fmtRelative(iso: string): string {
  const delta = Date.now() - new Date(iso).getTime();
  if (delta < 60_000)  return `${Math.round(delta / 1000)}s ago`;
  if (delta < 3_600_000) return `${Math.round(delta / 60_000)}m ago`;
  return `${Math.round(delta / 3_600_000)}h ago`;
}

export function fmtEta(crossingUnixSec: number): string {
  const delta = crossingUnixSec * 1000 - Date.now();
  if (delta <= 0) return "now";
  const mins = Math.round(delta / 60_000);
  if (mins < 60) return `~${mins}min`;
  const hrs = (delta / 3_600_000).toFixed(1);
  return `~${hrs}h`;
}

export function fmtPercent(ratio: number): string {
  return `${(ratio * 100).toFixed(1)}%`;
}
