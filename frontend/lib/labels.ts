export function label(value: string) {
  const labels: Record<string, string> = {
    DEGRADED: "Partial result", STOPPING: "Stopping",
    VULNERABLE: "Needs attention", UNSUPPORTED: "Support not established", STRONG: "Support found",
    MIXED: "Needs qualification", INSUFFICIENT_EVIDENCE: "More evidence needed",
    NO_COUNTER_AUTHORITY: "No contrary authority found in searched sources", CONTRADICTED: "Possible contradiction",
    UNKNOWN: "Not yet assessed", NOT_REVIEWED: "Not reviewed", SUFFICIENT: "Sufficient within searched sources",
  };
  return labels[value.toUpperCase()] ?? value.toLowerCase().replaceAll("_", " ").replace(/^./, c => c.toUpperCase());
}
