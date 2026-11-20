// Pure logic for reading status.json – no React, so it can be unit-tested with node:test.

export type StockStatus = "AVAILABLE" | "POSSIBLY_AVAILABLE" | "UNAVAILABLE" | "UNKNOWN";
export type MonitorState = "active" | "paused" | "error";

export interface StatusFile {
  schema: number;
  generated_at: string;
  product: { name: string; ref: string; material: string; url: string; region: string };
  monitoring: {
    state: MonitorState;
    message: string;
    interval_minutes: number;
    stale_after: string;
    consecutive_unknown?: number;
    last_ok_check_at?: string | null;
    health_alert_active?: boolean;
  };
  stock: { status: StockStatus; label: string; checked_at: string | null; evidence: string[] };
  notifications: {
    configured: boolean;
    last_sent_at: string | null;
    last_kind: string | null;
    last_error_at: string | null;
    last_test_ok_at: string | null;
    last_test_error_at: string | null;
  };
}

export interface View {
  monitor: MonitorState;
  monitorLabel: string;
  monitorMessage: string;
  stock: StockStatus;
  stockLabel: string;
  lastKnownStock: StockStatus | null; // shown when data is stale
  stale: boolean;
  data: StatusFile | null;
}

const STOCK_LABEL: Record<StockStatus, string> = {
  AVAILABLE: "Available",
  POSSIBLY_AVAILABLE: "Possibly Available",
  UNAVAILABLE: "Unavailable",
  UNKNOWN: "Unknown",
};
const MONITOR_LABEL: Record<MonitorState, string> = {
  active: "Monitoring Active",
  paused: "Monitoring Paused",
  error: "Monitoring Error",
};

const isStr = (v: unknown): v is string => typeof v === "string";

export function parseStatus(raw: unknown): StatusFile {
  const r = raw as Partial<StatusFile> | null;
  if (!r || typeof r !== "object" || r.schema !== 1) throw new Error("status.json: unknown format");
  if (!r.stock || !(r.stock.status in STOCK_LABEL)) throw new Error("status.json: bad stock status");
  if (!r.monitoring || !(r.monitoring.state in MONITOR_LABEL)) throw new Error("status.json: bad monitoring state");
  if (!isStr(r.generated_at) || !isStr(r.monitoring.stale_after)) throw new Error("status.json: missing timestamps");
  if (Number.isNaN(Date.parse(r.monitoring.stale_after))) throw new Error("status.json: bad stale_after");
  return r as StatusFile;
}

export function buildView(data: StatusFile | null, now: Date, loadError?: string): View {
  if (!data) {
    return {
      monitor: "error",
      monitorLabel: MONITOR_LABEL.error,
      monitorMessage: loadError || "无法读取监控状态数据",
      stock: "UNKNOWN",
      stockLabel: STOCK_LABEL.UNKNOWN,
      lastKnownStock: null,
      stale: false,
      data: null,
    };
  }
  const stale = now.getTime() > Date.parse(data.monitoring.stale_after);
  if (stale && data.monitoring.state !== "paused") {
    // Never present an old result as current.
    return {
      monitor: "error",
      monitorLabel: MONITOR_LABEL.error,
      monitorMessage: `状态数据已过期：最近一次更新于 ${formatET(data.generated_at)}，监控可能没有按时运行。`,
      stock: "UNKNOWN",
      stockLabel: STOCK_LABEL.UNKNOWN,
      lastKnownStock: data.stock.status,
      stale: true,
      data,
    };
  }
  return {
    monitor: data.monitoring.state,
    monitorLabel: MONITOR_LABEL[data.monitoring.state],
    monitorMessage: data.monitoring.message,
    stock: data.stock.status,
    stockLabel: STOCK_LABEL[data.stock.status],
    lastKnownStock: null,
    stale,
    data,
  };
}

export async function fetchStatus(url: string, fetchImpl: typeof fetch = fetch): Promise<StatusFile> {
  const sep = url.includes("?") ? "&" : "?";
  const res = await fetchImpl(`${url}${sep}t=${Date.now()}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`无法读取状态数据 (HTTP ${res.status})`);
  return parseStatus(await res.json());
}

export function formatET(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(d);
}

export function relative(iso: string | null | undefined, now: Date): string {
  if (!iso) return "";
  const mins = Math.round((now.getTime() - Date.parse(iso)) / 60000);
  if (Number.isNaN(mins)) return "";
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const h = Math.floor(mins / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.floor(h / 24)} days ago`;
}
