/* market-sniper 弹窗：开关 / 周期 / 服务地址 + 连接状态 */
const DEFAULTS = { server: "http://127.0.0.1:7132", tf: "1d", enabled: true };

const $ = (id) => document.getElementById(id);

function fill(cfg) {
  $("enabled").checked = !!cfg.enabled;
  $("tf").value = cfg.tf;
  $("server").value = cfg.server;
}

document.addEventListener("DOMContentLoaded", () => {
  chrome.storage.sync.get(DEFAULTS, fill);

  $("save").addEventListener("click", () => {
    const cfg = {
      enabled: $("enabled").checked,
      tf: $("tf").value,
      server: $("server").value.trim().replace(/\/+$/, "") || DEFAULTS.server,
    };
    chrome.storage.sync.set(cfg, () => {
      $("status").textContent = "已保存 ✓";
      checkHealth(cfg.server);
    });
  });

  chrome.storage.sync.get(DEFAULTS, (cfg) => checkHealth(cfg.server));
});

async function checkHealth(server) {
  try {
    const r = await fetch(`${server}/api/health`,
                          { signal: AbortSignal.timeout(4000) });
    const j = await r.json();
    $("status").textContent =
      `桌面程序在线 ✓ v${j.version || "?"}（${j.service}）`;
  } catch (e) {
    $("status").textContent = "桌面程序未启动（先运行 market-sniper GUI）";
  }
}
