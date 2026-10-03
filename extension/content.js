/* market-sniper 内容脚本：识别页面标的 → 查本地 API → 图表上叠加信号浮层。
 * 适配：TradingView / Binance / Yahoo财经 / 雪球。
 * 计算全部在本地桌面程序（http://127.0.0.1:7132），插件只做展示。
 */
(() => {
  "use strict";

  const DEFAULTS = { server: "http://127.0.0.1:7132", tf: "1d", enabled: true };
  let cfg = { ...DEFAULTS };

  try {
    chrome.storage.sync.get(DEFAULTS, (c) => { cfg = { ...cfg, ...c }; refresh(); });
    chrome.storage.onChanged.addListener((ch) => {
      for (const k of Object.keys(DEFAULTS)) if (ch[k]) cfg[k] = ch[k].newValue;
      refresh();
    });
  } catch (e) { /* storage 不可用时用默认配置 */ }

  // ---------------- 标的识别 ----------------
  function detectSymbol() {
    const href = location.href;
    let m;
    // TradingView: ?symbol=BINANCE%3ABTCUSDT / NASDAQ%3AAAPL
    m = href.match(/[?&]symbol=([A-Za-z0-9_%]+(?:%3A|:)[A-Za-z0-9_.\-]+)/);
    if (m) return { raw: decodeURIComponent(m[1]), site: "tradingview" };
    // Binance: /trade/BTC_USDT /futures/BTCUSDT
    m = href.match(/\/(?:trade|futures)\/([A-Za-z0-9]+?)(?:_(USDT|USDC|FDUSD|BUSD|USD))(?:[/?#]|$)/);
    if (m) return { raw: m[1].toUpperCase() + "/" + m[2].toUpperCase(), site: "binance" };
    m = href.match(/\/(?:trade|futures)\/([A-Z0-9]{4,12})(?:[/?#]|$)/);
    if (m) return { raw: m[1].toUpperCase(), site: "binance" };
    // 长桥: /stock/00700-HK /quotes/AAPL-US /trade/HK.00700（可带语言前缀）
    m = href.match(/\/(?:stock|quotes?|trade)\/([A-Za-z0-9]{1,6})-(HK|US|SG|CN|JP)(?:[/?#]|$)/);
    if (m) return { raw: m[1].toUpperCase() + "-" + m[2].toUpperCase(), site: "longbridge" };
    m = href.match(/\/(?:stock|quotes?|trade)\/(HK|US|SG|CN|JP)\.([A-Za-z0-9]{1,6})(?:[/?#]|$)/);
    if (m) return { raw: m[1].toUpperCase() + "." + m[2].toUpperCase(), site: "longbridge" };
    // Yahoo: /quote/AAPL / /quote/0700.HK
    m = href.match(/\/quote\/([A-Za-z0-9.\-]+)/);
    if (m) return { raw: m[1].toUpperCase(), site: "yahoo" };
    // 雪球: /S/00700 /S/SH600519 /S/AAPL
    m = href.match(/\/S\/([A-Za-z0-9.]+)/);
    if (m) return { raw: m[1].toUpperCase(), site: "xueqiu" };
    return null;
  }

  const QUOTES = ["USDT", "USDC", "FDUSD", "TUSD", "BUSD", "USD"];

  // 远程代码 → 本地 MARKET:CODE；返回 null = 无法支持（A股/新加坡等）
  function toLocalCode(raw) {
    let s = String(raw || "").trim().toUpperCase();
    if (s.includes(":")) s = s.split(":").pop();
    if (s.includes("/")) return "CRYPTO:" + s;
    // 长桥形态：00700-HK / AAPL-US / HK.00700 / US.AAPL
    let m = s.match(/^([A-Z0-9]{1,6})-(HK|US|SG|CN|JP)$/);
    if (m) {
      if (m[2] === "HK") return "HK:" + m[1].padStart(5, "0");
      if (m[2] === "US") return "US:" + m[1];
      return null;
    }
    m = s.match(/^(HK|US|SG|CN|JP)\.([A-Z0-9]{1,6})$/);
    if (m) {
      if (m[1] === "HK") return "HK:" + m[2].padStart(5, "0");
      if (m[1] === "US") return "US:" + m[2];
      return null;
    }
    if (s.endsWith(".HK")) return "HK:" + s.slice(0, -3).padStart(5, "0");
    if (/^\d{1,5}$/.test(s)) return "HK:" + s.padStart(5, "0");
    if (/^(SH|SZ|BJ)/.test(s) && /^\d{6}$/.test(s.slice(2))) return null;
    for (const q of QUOTES) {
      if (s.endsWith(q) && s.length > q.length)
        return "CRYPTO:" + s.slice(0, -q.length) + "/" + q;
    }
    if (/^[A-Z][A-Z0-9.\-]{0,5}$/.test(s)) return "US:" + s;
    return null;
  }

  // ---------------- API ----------------
  async function fetchSignal(local) {
    const url = `${cfg.server}/api/signal?code=${encodeURIComponent(local)}` +
                `&tf=${encodeURIComponent(cfg.tf)}&limit=50`;
    const r = await fetch(url, { signal: AbortSignal.timeout(6000) });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.error || "HTTP " + r.status);
    }
    return r.json();
  }

  // ---------------- 浮层 ----------------
  let host = null, chip = null;

  function ensureChip() {
    if (chip && host.isConnected) return;
    host = document.createElement("div");
    host.id = "market-sniper-host";
    const root = host.attachShadow({ mode: "open" });
    chip = document.createElement("div");
    chip.style.cssText = [
      "font:12px/1.6 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif",
      "background:rgba(8,10,14,.93)", "color:#d7dee6",
      "border:1px solid #3a3a3a", "border-radius:8px",
      "padding:8px 12px", "min-width:200px", "max-width:340px",
      "box-shadow:0 4px 18px rgba(0,0,0,.55)", "pointer-events:auto"
    ].join(";");
    root.appendChild(chip);
    document.documentElement.appendChild(host);
  }

  // 尽量贴到页面最大的 K 线画布右上角；找不到就固定右下角
  function anchorToChart() {
    if (!host) return;
    let best = null, bestArea = 0;
    for (const c of document.querySelectorAll("canvas")) {
      const r = c.getBoundingClientRect();
      const a = r.width * r.height;
      if (r.width > 420 && r.height > 260 && a > bestArea) { best = r; bestArea = a; }
    }
    if (best) {
      host.style.left = Math.max(8, Math.min(best.right - 350, window.innerWidth - 358)) + "px";
      host.style.top = Math.max(8, best.top + 8) + "px";
    } else {
      host.style.left = "auto";
      host.style.top = "auto";
    }
    host.style.position = "fixed";
    host.style.right = best ? "auto" : "16px";
    host.style.bottom = best ? "auto" : "16px";
    host.style.zIndex = "2147483647";
  }

  const UP = "#ff5252", DOWN = "#00e676", DIM = "#8fa0ad";

  // 仓位动作展示：dir=up 红▲ / down 绿▼，hollow=空心（平仓动作）
  const ACTIONS = {
    open_long:   { label: "开多", dir: "up",   hollow: false },
    close_long:  { label: "平多", dir: "down", hollow: true },
    open_short:  { label: "开空", dir: "down", hollow: false },
    close_short: { label: "平空", dir: "up",   hollow: true },
    buy:         { label: "买",   dir: "up",   hollow: false },
    sell:        { label: "卖",   dir: "down", hollow: false },
  };
  const meta = (side) => ACTIONS[side] || { label: side, dir: "up", hollow: false };

  function fmtTime(ms) {
    if (!ms) return "-";
    const d = new Date(Number(ms));
    if (isNaN(d.getTime())) return String(ms);
    const p = (x) => String(x).padStart(2, "0");
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function renderLoading(local) {
    return `<div style="color:${DIM}"><b style="color:#ffee00">⚡ market-sniper</b> · ${local} · ${cfg.tf}</div>
            <div style="color:${DIM}">加载中…</div>`;
  }

  function renderSignals(d) {
    const sigs = d.signals || [];
    const last = sigs[sigs.length - 1];
    let html = `<div style="color:${DIM}"><b style="color:#ffee00">⚡ market-sniper</b> · ${d.code} · ${d.tf} · BOLL+ATR</div>`;
    if (last) {
      const m = meta(last.side);
      const col = m.dir === "up" ? UP : DOWN;
      const price = Number(last.price), stop = Number(last.stop);
      const now = Number(d.last_close) || 0;
      const pct = now && price ? (now - price) / price * 100 : 0;
      const arrow = m.dir === "up" ? "▲" : "▼";
      html += `<div style="font-size:15px;font-weight:700;color:${col};margin:2px 0">
                 ${m.hollow ? "△" : arrow} ${m.label} @ ${price.toFixed(price > 100 ? 2 : 4)}</div>
               <div style="color:${DIM}">现价 ${now.toFixed(now > 100 ? 2 : 4)}
                 <span style="color:${pct >= 0 ? UP : DOWN}">${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%</span>
                 ${last.stop ? ` · 止损参考 ${stop.toFixed(stop > 100 ? 2 : 4)}` : ""}</div>
               <div style="color:${DIM}">信号时间 ${fmtTime(last.ts_ms)}
                 · 当前仓位 <b style="color:#ffee00">${d.position || "空仓"}</b></div>`;
    } else {
      html += `<div style="color:${DIM};margin:2px 0">暂无信号（带内整理）</div>`;
    }
    if (sigs.length > 1) {
      html += `<div style="margin-top:4px;color:${DIM};border-top:1px solid #2a2a2a;padding-top:4px">`
        + sigs.slice(-6).reverse().map((s) => {
            const m2 = meta(s.side);
            return `<span style="color:${m2.dir === "up" ? UP : DOWN}">${m2.hollow ? "△" : (m2.dir === "up" ? "▲" : "▼")}</span>${m2.label}${fmtTime(s.ts_ms)}`;
          }).join(" · ") + `</div>`;
    }
    return html;
  }

  // ---------------- 主循环 ----------------
  let busy = false;

  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      ensureChip();
      anchorToChart();
      if (!cfg.enabled) { host.style.display = "none"; return; }
      host.style.display = "";

      const det = detectSymbol();
      if (!det) {
        chip.innerHTML = `<b style="color:#ffee00">⚡ market-sniper</b> <span style="color:${DIM}">未识别标的</span>`;
        return;
      }
      const local = toLocalCode(det.raw);
      if (!local) {
        chip.innerHTML = `<b style="color:#ffee00">⚡ market-sniper</b> · ${det.raw}
                          <div style="color:${DIM}">该市场暂不支持（A股/新加坡等）</div>`;
        return;
      }
      chip.innerHTML = renderLoading(local);
      try {
        const d = await fetchSignal(local);
        chip.innerHTML = renderSignals(d);
      } catch (e) {
        chip.innerHTML = `<b style="color:#ffee00">⚡ market-sniper</b> · ${local}
          <div style="color:${DIM}">连不上本地程序：${e.message}</div>
          <div style="color:${DIM}">先启动 market-sniper GUI（API ${cfg.server}）</div>`;
      }
    } finally {
      busy = false;
    }
  }

  let lastHref = location.href;
  setInterval(() => {
    if (location.href !== lastHref) { lastHref = location.href; }
    refresh();
  }, 4000);
  window.addEventListener("resize", () => { if (host) anchorToChart(); });
  refresh();
})();
