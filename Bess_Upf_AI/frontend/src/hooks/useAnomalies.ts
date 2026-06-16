import { useQuery } from "@tanstack/react-query";
import { fetchAnomalies, Credentials } from "../api/client";
import { POLL_INTERVALS } from "../lib/constants";

export function useAnomalies(
  creds: Credentials | null,
  opts: { since?: number; severity?: string; metric?: string } = {}
) {
  return useQuery({
    queryKey: ["anomalies", opts.since, opts.severity, opts.metric],
    queryFn: () => fetchAnomalies(creds!, opts.since, opts.severity),
    enabled: !!creds,
    refetchInterval: POLL_INTERVALS.anomalies,
    staleTime: POLL_INTERVALS.anomalies / 2,
    placeholderData: (prev) => prev,
  });
}
