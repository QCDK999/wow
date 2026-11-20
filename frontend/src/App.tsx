import { useCallback, useEffect, useState, type ReactNode } from "react";
import { buildView, fetchStatus, formatET, relative, type StatusFile, type StockStatus, type View } from "./status.ts";

const STATUS_URL: string = import.meta.env.VITE_STATUS_URL ?? "";
const REFRESH_MS = 5 * 60 * 1000;

const STOCK_STYLE: Record<StockStatus, { dot: string; text: string; note: string }> = {
  AVAILABLE: { dot: "bg-emerald-600", text: "text-emerald-800", note: "已确认可以在美国官网在线购买" },
  POSSIBLY_AVAILABLE: { dot: "bg-gold", text: "text-gold-deep", note: "出现补货信号，尚未完全验证，请人工确认" },
  UNAVAILABLE: { dot: "bg-stone-400", text: "text-ink", note: "目前无法在线购买（官网仅提供电话订购）" },
  UNKNOWN: { dot: "bg-stone-300", text: "text-stone-500", note: "暂时无法可靠判断库存状态" },
};

const MONITOR_DOT = { active: "bg-emerald-600", paused: "bg-stone-400", error: "bg-red-700" } as const;

export default function App() {
  const [data, setData] = useState<StatusFile | null>(null);
  const [error, setError] = useState<string | undefined>();
  const [loading, setLoading] = useState(true);
  const [now, setNow] = useState(() => new Date());

  const load = useCallback(async () => {
    if (!STATUS_URL) {
      setError("未配置状态数据地址 (VITE_STATUS_URL)");
      setLoading(false);
      return;
    }
    try {
      setData(await fetchStatus(STATUS_URL));
      setError(undefined);
    } catch (e) {
      setData(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
      setNow(new Date());
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, REFRESH_MS);
    const tick = setInterval(() => setNow(new Date()), 30_000);
    const onVis = () => document.visibilityState === "visible" && load();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      clearInterval(id);
      clearInterval(tick);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [load]);

  const view = buildView(data, now, error);

  return (
    <div className="min-h-screen bg-ivory text-ink">
      <div className="mx-auto max-w-3xl px-5 py-10 sm:py-16">
        <header className="mb-10 text-center sm:mb-14">
          <div className="mx-auto mb-5 h-px w-12 bg-gold" />
          <h1 className="font-serif text-[26px] tracking-[0.12em] uppercase sm:text-4xl sm:tracking-[0.18em]">VCA Stock Monitor</h1>
          <p className="mt-3 text-xs tracking-[0.3em] text-stone-500 uppercase">United States · Online Boutique</p>
        </header>

        {loading ? (
          <p className="text-center text-sm text-stone-500">Loading…</p>
        ) : (
          <main className="space-y-6">
            <ProductCard data={data} />
            <StockCard view={view} now={now} />
            <div className="grid gap-4 sm:grid-cols-2">
              <MonitorTile view={view} now={now} />
              <Tile label="Last check">
                <p className="text-base">{formatET(data?.stock.checked_at)}</p>
                <p className="mt-1 text-xs text-stone-500">{relative(data?.stock.checked_at, now)}</p>
              </Tile>
              <Tile label="Check interval">
                <p className="text-base">
                  {data ? `Every ${formatInterval(data.monitoring.interval_minutes)}` : "—"}
                </p>
                <p className="mt-1 text-xs text-stone-500">GitHub Actions 定时任务，可能有几分钟延迟</p>
              </Tile>
              <EmailTile data={data} />
            </div>
          </main>
        )}

        <footer className="mt-12 space-y-2 text-center text-xs leading-relaxed text-stone-500">
          <p>
            Status file generated {formatET(data?.generated_at)} · refreshes every 5 minutes ·{" "}
            <button onClick={load} className="underline decoration-gold underline-offset-4 hover:text-ink">
              refresh now
            </button>
          </p>
          <p>库存状态可能随时变化，最终以官网实际结账结果为准。本站与 Van Cleef &amp; Arpels 无关联。</p>
        </footer>
      </div>
    </div>
  );
}

function formatInterval(m: number) {
  return m % 60 === 0 ? (m === 60 ? "hour" : `${m / 60} hours`) : `${m} minutes`;
}

function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`border border-line bg-white px-6 py-6 sm:px-8 ${className}`}>{children}</section>;
}

function Label({ children }: { children: ReactNode }) {
  return <p className="mb-3 text-[11px] font-medium tracking-[0.25em] text-stone-500 uppercase">{children}</p>;
}

function Tile({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Card>
      <Label>{label}</Label>
      {children}
    </Card>
  );
}

function ProductCard({ data }: { data: StatusFile | null }) {
  const p = data?.product ?? {
    name: "Vintage Alhambra Bracelet, 5 Motifs",
    ref: "VCARD34700",
    material: "18K White Gold / Chalcedony",
    url: "https://www.vancleefarpels.com/us/en/collections/jewelry/alhambra/vcard34700---vintage-alhambra-bracelet-5-motifs.html",
  };
  return (
    <Card className="text-center">
      <Label>The piece</Label>
      <h2 className="font-serif text-2xl sm:text-3xl">{p.name}</h2>
      <p className="mt-2 text-sm text-stone-600">
        Ref. {p.ref} · {p.material}
      </p>
      <a
        href={p.url}
        target="_blank"
        rel="noopener noreferrer"
        className="mt-5 inline-block border border-gold px-5 py-2 text-xs tracking-[0.2em] text-gold-deep uppercase transition hover:bg-gold hover:text-white"
      >
        View on vancleefarpels.com
      </a>
    </Card>
  );
}

function StockCard({ view, now }: { view: View; now: Date }) {
  const s = STOCK_STYLE[view.stock];
  const evidence = view.stale ? [] : (view.data?.stock.evidence ?? []);
  return (
    <Card className="text-center">
      <Label>Online availability</Label>
      <div className="flex items-center justify-center gap-3">
        <span className={`h-2.5 w-2.5 rounded-full ${s.dot}`} />
        <span className={`font-serif text-4xl sm:text-5xl ${s.text}`}>{view.stockLabel}</span>
      </div>
      <p className="mt-3 text-sm text-stone-600">{s.note}</p>
      {view.stale && view.lastKnownStock && (
        <p className="mt-2 text-xs text-red-800">
          数据已过期。最后已知状态：{view.lastKnownStock}（{relative(view.data?.stock.checked_at, now)}）
        </p>
      )}
      {evidence.length > 0 && (
        <details className="mx-auto mt-5 max-w-md text-left text-xs text-stone-600">
          <summary className="cursor-pointer text-center tracking-wide text-stone-500 hover:text-ink">检测依据</summary>
          <ul className="mt-3 space-y-1 border-t border-line pt-3">
            {evidence.map((e) => (
              <li key={e}>· {e}</li>
            ))}
          </ul>
        </details>
      )}
    </Card>
  );
}

function MonitorTile({ view, now }: { view: View; now: Date }) {
  const m = view.data?.monitoring;
  const showLastOk = view.monitor === "error" && m?.last_ok_check_at;
  return (
    <Tile label="Monitoring">
      <p className="flex items-center gap-2 text-base">
        <span className={`h-2 w-2 rounded-full ${MONITOR_DOT[view.monitor]}`} />
        {view.monitorLabel}
      </p>
      <p className="mt-1 text-xs text-stone-500">{view.monitorMessage}</p>
      {showLastOk && (
        <p className="mt-1 text-xs text-stone-500">
          最近一次成功检测：{formatET(m?.last_ok_check_at)}（{relative(m?.last_ok_check_at, now)}）
        </p>
      )}
      {m?.health_alert_active && !view.stale && (
        <p className="mt-1 text-xs text-red-800">已发送监控失效提醒邮件；恢复前可能收不到补货通知。</p>
      )}
    </Tile>
  );
}

function EmailTile({ data }: { data: StatusFile | null }) {
  const n = data?.notifications;
  const testOk = n?.last_test_ok_at && (!n.last_test_error_at || n.last_test_ok_at > n.last_test_error_at);
  const sendFailed = n?.last_error_at && (!n.last_sent_at || n.last_error_at > n.last_sent_at);
  let line = "—";
  let sub = "";
  if (n) {
    line = n.configured ? "Configured" : "Not configured";
    if (sendFailed) sub = `最近一次发送失败：${formatET(n.last_error_at)}`;
    else if (testOk) sub = `测试邮件成功：${formatET(n.last_test_ok_at)}`;
    else if (n.last_test_error_at) sub = `测试邮件失败：${formatET(n.last_test_error_at)}`;
    else if (n.configured) sub = "已配置，尚未发送测试邮件验证";
    else sub = "请在 GitHub Secrets 中配置 SMTP";
    if (n.last_sent_at) sub += ` · 上次通知 ${formatET(n.last_sent_at)}`;
  }
  return (
    <Tile label="Email notifications">
      <p className="flex items-center gap-2 text-base">
        <span className={`h-2 w-2 rounded-full ${n?.configured && !sendFailed ? "bg-emerald-600" : "bg-red-700"}`} />
        {line}
      </p>
      <p className="mt-1 text-xs text-stone-500">{sub}</p>
    </Tile>
  );
}
