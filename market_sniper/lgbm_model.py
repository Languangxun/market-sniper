# -*- coding: utf-8 -*-
"""LightGBM 信号推理（港股）：日线一套 + 分时(5m)一套，按图表周期自动选。

模型由 scripts/train_hk_lgbm.py 产出：
    data/models/hk/daily/index.json + *.txt      日线（未来 5 日涨跌）
    data/models/hk/intraday/index.json + *.txt   分时 5m（未来 12 根涨跌）

增量数据领先训练 data_last ≥ retrain_days（默认 5）时，后台自动重训；
重训期间前台继续用旧模型（index.json 最后原子替换）。
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import subprocess
import sys
import threading
import time

import numpy as np

from market_sniper import features as feat
from market_sniper.data import db

log = logging.getLogger("market_sniper.lgbm")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_ROOT = os.path.join(_ROOT, "data", "models", "hk")
KINDS = ("daily", "intraday")

_lock = threading.Lock()
_cache: dict[str, dict] = {
    "daily": {"mtime": None, "index": None, "by_code": {}, "boosters": {}},
    "intraday": {"mtime": None, "index": None, "by_code": {}, "boosters": {}},
}
_retrain_last_check = 0.0

# 发布包内置模型缓存：首次使用自动解包到 data/models/hk/
CACHE_ARCHIVES = (
    os.path.join(_ROOT, "data", "models_hk.tar.gz"),
    os.path.join(_ROOT, "data", "models", "hk_cache.tar.gz"),
    os.path.join(_ROOT, "dist", "lgbm_hk_models.tar.gz"),
)


def ensure_cache() -> bool:
    """模型目录缺失时，从内置缓存包解出（返回是否有可用模型）。"""
    if all(available(k) for k in KINDS):
        return True
    for path in CACHE_ARCHIVES:
        if not os.path.exists(path):
            continue
        try:
            import tarfile
            os.makedirs(os.path.dirname(MODELS_ROOT), exist_ok=True)
            with tarfile.open(path, "r:gz") as tf:
                tf.extractall(os.path.dirname(MODELS_ROOT),
                              filter="data")
            log.info("已从缓存注入 LightGBM 模型: %s", path)
            return all(available(k) for k in KINDS)
        except Exception as e:
            log.warning("解包模型缓存失败 %s: %s", path, e)
    return False


def available(kind: str = "daily") -> bool:
    return os.path.exists(os.path.join(MODELS_ROOT, kind, "index.json"))


def _index(kind: str) -> dict | None:
    if kind not in _cache:
        return None
    if not available(kind):
        ensure_cache()
    path = os.path.join(MODELS_ROOT, kind, "index.json")
    if not os.path.exists(path):
        return None
    mtime = os.path.getmtime(path)
    c = _cache[kind]
    if c["index"] is not None and c["mtime"] == mtime:
        return c["index"]
    with _lock:
        try:
            with open(path, encoding="utf-8") as f:
                index = json.load(f)
        except (OSError, ValueError) as e:
            log.warning("加载 %s 模型索引失败: %s", kind, e)
            return None
        by_code = {}
        for entry in index.get("models", []):
            for code in entry.get("codes") or []:
                by_code[code] = entry
        c.update({"index": index, "mtime": mtime, "by_code": by_code,
                  "boosters": {}})
    return index


def _entry_for(kind: str, code: str) -> dict | None:
    if not _index(kind):
        return None
    entry = _cache[kind]["by_code"].get(code)
    if entry:
        return entry
    for e in _cache[kind]["index"].get("models", []):
        if e.get("id") == "global":
            return e
    return None


def _booster(kind: str, entry: dict):
    import lightgbm as lgb

    key = entry.get("id")
    c = _cache[kind]
    b = c["boosters"].get(key)
    if b is not None:
        return b
    fname = entry.get("file")
    if not fname:
        return None
    path = os.path.join(MODELS_ROOT, kind, fname)
    if not os.path.exists(path):
        return None
    with _lock:
        b = c["boosters"].get(key)
        if b is None:
            b = lgb.Booster(model_file=path)
            c["boosters"][key] = b
    return b


def _thresholds(entry: dict, enter, exit_) -> tuple[float, float]:
    """按模型验证集分位数自适应：入场=90分位，出场=50分位。"""
    q = entry.get("prob_q") or []
    med = q[0] if len(q) > 0 else 0.50
    hi = q[3] if len(q) > 3 else 0.60
    if enter is None:
        enter = max(hi, med + 0.02)
    if exit_ is None:
        exit_ = med
    if exit_ >= enter:
        exit_ = enter - 0.02
    return float(enter), float(exit_)


def _state_machine(dates, probs, closes, kind, enter, exit_):
    from market_sniper.signals import ts_to_ms

    out = []
    pos = "flat"
    label = "日线" if kind == "daily" else "分时"
    for i, dt in enumerate(dates):
        p = probs[i]
        if not np.isfinite(p):
            continue
        if pos == "flat" and p >= enter:
            side = "buy"
        elif pos == "long" and p <= exit_:
            side = "sell"
        else:
            continue
        pos = "long" if side == "buy" else "flat"
        out.append({
            "ts": dt, "ts_ms": ts_to_ms(dt) or 0, "side": side,
            "price": round(float(closes[i]), 6),
            "prob": round(float(p), 4),
            "reason": f"LGBM{label} p={p:.2f}",
        })
    return out, pos


def run_algo(bars: dict, market: str = "HK", timeframe: str | None = None,
             *, enter: float | None = None, exit: float | None = None,
             max_signals: int = 200, **params) -> dict:
    """SIGNAL_ALGOS 兼容签名。仅支持港股；日线/分钟自动选模型。"""
    empty = {"algo": "lgbm", "signals": [], "position": "空仓",
             "last_close": None, "bars": 0}
    if market != "HK" or not bars:
        empty["note"] = "LightGBM 模型目前仅支持港股"
        return empty
    kind = timeframe if timeframe in KINDS else \
        ("daily" if bars.get("dates") else "intraday")
    code = str(bars.get("code") or "")
    _maybe_retrain()
    entry = _entry_for(kind, code)
    if entry is None:
        empty["note"] = f"{kind} 模型未训练（跑 scripts/train_hk_lgbm.py）"
        return empty
    booster = _booster(kind, entry)
    if booster is None:
        empty["note"] = f"缺少 {kind} 模型文件"
        return empty

    if kind == "daily":
        series = db.load_daily_bars("HK", code) if code else bars
    else:
        series = db.load_min_bars("HK", code, "5m") if code else None
    if not series or len(series.get("close", [])) < 100:
        empty["note"] = "本地数据不足（需要先回填该周期K线）"
        return empty

    peer = entry.get("peer") or {}
    default = peer[sorted(peer)[-1]] if peer else None
    names, X, dates = feat.build_features(series, peer=peer,
                                          peer_default=default)
    warm = np.isfinite(X[:, names.index("ma60_ratio")]) & \
        np.isfinite(X[:, names.index("rsi_14")])
    probs = np.full(len(dates), np.nan)
    if warm.any():
        probs[warm] = booster.predict(X[warm])

    enter_thr, exit_thr = _thresholds(entry, enter, exit)
    signals, pos = _state_machine(dates, probs, series["close"], kind,
                                  enter_thr, exit_thr)
    return {
        "algo": "lgbm",
        "params": {"kind": kind, "enter": enter_thr, "exit": exit_thr},
        "signals": signals[-max_signals:],
        "last_close": round(float(series["close"][-1]), 6),
        "bars": len(dates),
        "position": "多头" if pos == "long" else "空仓",
        "industry": entry.get("industry"),
        "model_id": entry.get("id"),
        "auc": entry.get("auc"),
        "valid_from": entry.get("valid_from"),
        "data_last": (_index(kind) or {}).get("data_last"),
    }


# ---------------- 增量自动重训 ----------------
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _spawn_retrain(lag_days: int) -> None:
    lock_path = os.path.join(MODELS_ROOT, ".retrain.lock")
    if os.path.exists(lock_path):
        try:
            pid = int((open(lock_path).read().strip() or "0"))
            if pid and _pid_alive(pid):
                return
        except (OSError, ValueError):
            pass
    script = os.path.join(_ROOT, "scripts", "train_hk_lgbm.py")
    log_dir = os.path.join(_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "lgbm_retrain.log")
    with open(log_path, "ab") as lf:
        proc = subprocess.Popen(
            [sys.executable, script, "--timeframe", "all"],
            cwd=_ROOT, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
            start_new_session=True)
    try:
        with open(lock_path, "w") as f:
            f.write(str(proc.pid))
    except OSError:
        pass
    log.info("数据领先训练 %d 天，后台重训已启动（pid=%d，前台继续用旧模型）",
             lag_days, proc.pid)


def _maybe_retrain() -> None:
    """懒检查：距上次训练数据新增 ≥ retrain_days 天则后台重训。"""
    global _retrain_last_check
    now = time.time()
    if now - _retrain_last_check < 600:
        return
    _retrain_last_check = now
    try:
        from market_sniper import config as config_mod
        cfg = config_mod.get_config()
        if not cfg.get("lgbm.auto_retrain", True):
            return
        days = int(cfg.get("lgbm.retrain_days", 5) or 5)
        trained = (_index("daily") or {}).get("data_last")
        with db.db_conn() as cx:
            row = cx.execute(
                "SELECT MAX(date) FROM daily_bars WHERE market='HK'").fetchone()
        cur = row[0] if row else None
        if not trained or not cur:
            return
        lag = (datetime.date.fromisoformat(cur) -
               datetime.date.fromisoformat(trained)).days
        if lag >= days:
            _spawn_retrain(lag)
    except Exception:
        log.exception("检查自动重训失败")


if __name__ == "__main__":
    import time as _t

    for code in ("00700", "03690", "09988"):
        daily = db.load_daily_bars("HK", code)
        five = db.load_min_bars("HK", code, "5m")
        for tf, bars in (("daily", daily), ("intraday", five)):
            t0 = _t.time()
            out = run_algo(bars or {"code": code}, market="HK", timeframe=tf)
            print(code, tf, f"{_t.time()-t0:.3f}s", out.get("industry"),
                  out.get("auc"), "signals:", len(out["signals"]),
                  out["position"], out.get("note", ""))
            for s in out["signals"][-2:]:
                print("   ", s["ts"], s["side"], s["price"], s["prob"])
