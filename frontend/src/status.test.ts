// Run with: npm test   (node --test, no extra dependencies)
import { test } from "node:test";
import assert from "node:assert/strict";
import { buildView, fetchStatus, parseStatus, type StatusFile } from "./status.ts";

const sample = (over: Partial<StatusFile["monitoring"]> = {}, stock = "UNAVAILABLE"): StatusFile =>
  parseStatus({
    schema: 1,
    generated_at: "2026-11-19T15:00:00+00:00",
    product: { name: "Vintage Alhambra Bracelet, 5 Motifs", ref: "VCARD34700", material: "18K White Gold / Chalcedony",
      url: "https://www.vancleefarpels.com/", region: "United States" },
    monitoring: { state: "active", message: "ok", interval_minutes: 60, stale_after: "2026-11-19T17:30:00+00:00", ...over },
    stock: { status: stock, label: "x", checked_at: "2026-11-19T15:00:00+00:00", evidence: [] },
    notifications: { configured: true, last_sent_at: null, last_kind: null, last_error_at: null,
      last_test_ok_at: null, last_test_error_at: null },
  });

test("fresh data is shown as-is", () => {
  const v = buildView(sample(), new Date("2026-11-19T16:00:00Z"));
  assert.equal(v.monitorLabel, "Monitoring Active");
  assert.equal(v.stockLabel, "Unavailable");
  assert.equal(v.stale, false);
});

test("16. stale data is never shown as current", () => {
  const v = buildView(sample({}, "AVAILABLE"), new Date("2026-11-19T18:00:00Z"));
  assert.equal(v.stale, true);
  assert.equal(v.monitor, "error");
  assert.equal(v.stock, "UNKNOWN");
  assert.equal(v.lastKnownStock, "AVAILABLE");
  assert.match(v.monitorMessage, /过期/);
});

test("paused stays paused even when old", () => {
  const v = buildView(sample({ state: "paused" }), new Date("2026-12-30T00:00:00Z"));
  assert.equal(v.monitorLabel, "Monitoring Paused");
});

test("error state from the monitor is passed through", () => {
  const v = buildView(sample({ state: "error", message: "邮件通知尚未配置" }), new Date("2026-11-19T16:00:00Z"));
  assert.equal(v.monitorLabel, "Monitoring Error");
  assert.equal(v.monitorMessage, "邮件通知尚未配置");
});

test("load failure shows error + unknown", () => {
  const v = buildView(null, new Date(), "HTTP 404");
  assert.equal(v.monitor, "error");
  assert.equal(v.stock, "UNKNOWN");
});

test("invalid files are rejected", () => {
  assert.throws(() => parseStatus({ schema: 2 }));
  assert.throws(() => parseStatus(null));
  assert.throws(() => parseStatus({ ...sample(), stock: { status: "MAYBE" } }));
});

test("fetch busts caches and surfaces HTTP errors", async () => {
  const seen: string[] = [];
  const ok = (async (u: string, init?: RequestInit) => {
    seen.push(u);
    assert.equal(init?.cache, "no-store");
    return new Response(JSON.stringify(sample()), { status: 200 });
  }) as typeof fetch;
  await fetchStatus("https://raw.githubusercontent.com/o/r/monitor-data/status.json", ok);
  assert.match(seen[0], /status\.json\?t=\d+$/);
  const bad = (async () => new Response("nope", { status: 404 })) as typeof fetch;
  await assert.rejects(fetchStatus("https://x/status.json", bad), /404/);
});

test("health fields from the monitor are optional and passed through", () => {
  const withHealth = sample({ state: "error", consecutive_unknown: 4, last_ok_check_at: "2026-11-19T10:00:00+00:00",
    health_alert_active: true });
  const v = buildView(withHealth, new Date("2026-11-19T16:00:00Z"));
  assert.equal(v.monitor, "error");
  assert.equal(v.data?.monitoring.consecutive_unknown, 4);
  assert.equal(buildView(sample(), new Date("2026-11-19T16:00:00Z")).data?.monitoring.health_alert_active, undefined);
});
