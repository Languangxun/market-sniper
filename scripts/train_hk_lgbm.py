#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""港股 LightGBM 训练：日线一套 + 分时(5m)一套（按行业分组，行业成员+自身数据）。

- 特征: market_sniper.features（28 维，含行业指数相对强弱）
- 行业: data/hk_industry.json（scripts/sync_hk_industry.py 生成）
- 标签: 未来 N 根收益 > 0；日线 N=5 天，分时 N=12 根（5m ≈ 1 小时）
- 每个行业训练一个模型；成员太少/样本不足并入全市场兜底
- 输出: data/models/hk/{daily,intraday}/index.json + 模型文件
  index.json 里 data_last 供增量≥N 日判重训；模型文件 .tmp 后原子替换

用法：
    python3 scripts/train_hk_lgbm.py                  # 日线+分时全量
    python3 scripts/train_hk_lgbm.py --timeframe daily
    python3 scripts/train_hk_lgbm.py --timeframe intraday --only 软件服务
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_sniper import features as feat
from market_sniper import indicators as ind_mod
from market_sniper.data import db

log = logging.getLogger("market_sniper.train_hk")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_ROOT = os.path.join(_ROOT, "data", "models", "hk")
INDUSTRY_JSON = os.path.join(_ROOT, "data", "hk_industry.json")

KINDS = {
    "daily":    {"native": "1d", "horizon": 5,  "min_bars": 300, "min_rows": 3000},
    "intraday": {"native": "5m", "horizon": 12, "min_bars": 500, "min_rows": 5000},
}
PEER_KEEP = 600
GLOBAL_SAMPLE = 400_000


def load_industry_map() -> dict[str, dict]:
    with open(INDUSTRY_JSON, encoding="utf-8") as f:
        return json.load(f)


def _load_bars(code: str, kind: str, min_bars: int) -> dict | None:
    if kind == "daily":
        bars = db.load_daily_bars("HK", code)
    else:
        bars = db.load_min_bars("HK", code, "5m")
    if not bars or len(bars["close"]) < min_bars:
        return None
    if np.any(~np.isfinite(bars["close"])) or np.any(bars["close"] <= 0):
        return None
    return bars


def _forward_label(close: np.ndarray, h: int) -> np.ndarray:
    n = len(close)
    y = np.full(n, np.nan)
    if n > h:
        fwd = close[h:] / close[:-h] - 1.0
        y[:-h] = (fwd > 0).astype(float)
    return y


def _dates(bars: dict) -> list[str]:
    return [str(x) for x in (bars.get("dates") or bars.get("ts") or [])]


def _last_date(bars_map: dict[str, dict]) -> str:
    mx = ""
    for bars in bars_map.values():
        ds = _dates(bars)
        if ds:
            mx = max(mx, str(ds[-1])[:10])
    return mx


def build_peer(codes: list[str], bars_map: dict[str, dict]) -> dict:
    """等权行业指数 + 宽度：{date: {peer_ret5, peer_ret20, peer_breadth}}。"""
    by_date: dict[str, list[float]] = defaultdict(list)
    for code in codes:
        bars = bars_map.get(code)
        if not bars:
            continue
        d = ind_mod.compute(bars)
        for dt, r in zip(_dates(bars), d["change_pct"]):
            if np.isfinite(r):
                by_date[dt].append(float(r))
    if not by_date:
        return {}
    dates = sorted(by_date)
    ret1 = np.array([float(np.mean(by_date[d])) for d in dates])
    breadth = np.array([float(np.mean([r > 0 for r in by_date[d]])) for d in dates])
    index = np.cumprod(1.0 + ret1)

    def back(arr, k):
        out = np.full(len(arr), np.nan)
        if len(arr) > k:
            out[k:] = arr[k:] / arr[:-k] - 1.0
        return out

    pr5, pr20 = back(index, 5), back(index, 20)
    out = {}
    for i in range(max(0, len(dates) - PEER_KEEP), len(dates)):
        row = {"peer_ret5": pr5[i], "peer_ret20": pr20[i],
               "peer_breadth": breadth[i]}
        out[dates[i]] = {k: (float(v) if np.isfinite(v) else None)
                         for k, v in row.items()}
    return out


def assemble(codes: list[str], bars_map: dict[str, dict], peer: dict,
             horizon: int):
    xs, ys, ds = [], [], []
    default = peer[sorted(peer)[-1]] if peer else None
    for code in codes:
        bars = bars_map.get(code)
        if not bars:
            continue
        names, X, dates = feat.build_features(bars, peer=peer,
                                              peer_default=default)
        close = np.asarray(bars["close"], dtype=np.float64)
        y = _forward_label(close, horizon)
        mask = np.isfinite(X[:, names.index("ma60_ratio")]) & \
            np.isfinite(X[:, names.index("rsi_14")]) & np.isfinite(y)
        if not mask.any():
            continue
        xs.append(X[mask])
        ys.append(y[mask])
        ds.extend([dates[i] for i in np.nonzero(mask)[0]])
    if xs:
        return np.vstack(xs), np.concatenate(ys), np.array(ds)
    return np.empty((0, len(feat.FEATURE_NAMES))), np.array([]), np.array([])


def train_one(X, y, dates, out_dir: str, tag: str):
    """返回 (fname, auc, prob_q, valid_from)；valid_from = 验证集起始日（样本外起点）。"""
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score

    if len(y) < 500 or len(np.unique(y)) < 2:
        return None, float("nan"), None, None
    params = dict(objective="binary", metric="auc", learning_rate=0.04,
                  num_leaves=31, subsample=0.8, subsample_freq=1,
                  colsample_bytree=0.8, min_child_samples=50,
                  reg_lambda=1.0, seed=7, num_threads=0, verbosity=-1)
    uniq = np.unique(dates)
    cut = uniq[max(1, int(len(uniq) * 0.85) - 1)]
    tr = dates <= cut
    va = dates > cut
    if tr.sum() < 300 or va.sum() < 50 or len(np.unique(y[tr])) < 2:
        tr, va = np.ones(len(y), bool), np.ones(len(y), bool)
    dtrain = lgb.Dataset(X[tr], label=y[tr])
    if tr.sum() != len(y):
        dvalid = lgb.Dataset(X[va], label=y[va], reference=dtrain)
        booster = lgb.train(params, dtrain, num_boost_round=800,
                            valid_sets=[dvalid],
                            callbacks=[lgb.early_stopping(40, verbose=False)])
    else:
        booster = lgb.train(params, dtrain, num_boost_round=300)
    fname = f"{tag}.txt"
    tmp = os.path.join(out_dir, fname + ".tmp")
    booster.save_model(tmp)
    os.replace(tmp, os.path.join(out_dir, fname))
    auc, prob_q, valid_from = float("nan"), None, None
    if tr.sum() != len(y):
        pv = booster.predict(X[va])
        auc = float(roc_auc_score(y[va], pv))
        prob_q = [round(float(np.quantile(pv, q)), 4)
                  for q in (0.5, 0.7, 0.8, 0.9, 0.95)]
        valid_from = str(min(dates[va]))[:10]
    return fname, auc, prob_q, valid_from


def slugify(text: str) -> str:
    s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", text or "")
    return s[:20] or "other"


def train_kind(kind: str, args, ind_map: dict) -> dict:
    cfg = KINDS[kind]
    horizon = cfg["horizon"]
    out_dir = os.path.join(MODELS_ROOT, kind)
    os.makedirs(out_dir, exist_ok=True)
    log.info("==== %s（%s，未来 %d 根）====", kind, cfg["native"], horizon)

    codes = [r["code"] for r in db.list_universe("HK", only_listed=False)]
    groups: dict[str, list[str]] = defaultdict(list)
    for code in codes:
        groups[(ind_map.get(code) or {}).get("industry") or ""].append(code)
    no_industry = groups.pop("", [])
    if args.only:
        groups = {k: v for k, v in groups.items() if k == args.only}
    log.info("行业 %d 个 / 标的 %d 只（无行业 %d）",
             len(groups), sum(len(v) for v in groups.values()), len(no_industry))

    entries: list[dict] = []
    gx, gy, gd = [], [], []
    data_last = ""
    rng = np.random.default_rng(7)

    def accumulate(X, y, d):
        if len(y) == 0:
            return
        take = min(len(y), 20_000)
        idx = (rng.choice(len(y), take, replace=False)
               if len(y) > take else slice(None))
        gx.append(X[idx])
        gy.append(y[idx])
        gd.append(d[idx])

    if no_industry:
        bars_map = {c: b for c in no_industry
                    if (b := _load_bars(c, kind, cfg["min_bars"])) is not None}
        if bars_map:
            X, y, d = assemble(list(bars_map), bars_map, None, horizon)
            accumulate(X, y, d)
            data_last = max(data_last, _last_date(bars_map))
            log.info("无行业 %d 只并入兜底（%d 只有数据）",
                     len(no_industry), len(bars_map))

    for gi, (industry, members) in enumerate(sorted(groups.items())):
        if args.limit:
            members = members[:args.limit]
        bars_map = {}
        for code in members:
            b = _load_bars(code, kind, cfg["min_bars"])
            if b is not None:
                bars_map[code] = b
        if not bars_map:
            continue
        peer = build_peer(list(bars_map), bars_map)
        X, y, d = assemble(list(bars_map), bars_map, peer, horizon)
        data_last = max(data_last, _last_date(bars_map))
        accumulate(X, y, d)
        tag = f"{gi}_{slugify(industry)}"
        if len(bars_map) < args.min_symbols or len(y) < cfg["min_rows"]:
            log.info("[%d/%d] %s：成员 %d / 样本 %d，并入兜底",
                     gi + 1, len(groups), industry, len(bars_map), len(y))
            continue
        fname, auc, prob_q, valid_from = train_one(X, y, d, out_dir, tag)
        if not fname:
            continue
        entries.append({
            "id": tag, "industry": industry, "codes": list(bars_map),
            "samples": len(y), "file": fname,
            "auc": None if not np.isfinite(auc) else round(auc, 4),
            "prob_q": prob_q, "valid_from": valid_from, "peer": peer,
        })
        log.info("[%d/%d] %s：%d 只 / %d 样本  AUC %.4f",
                 gi + 1, len(groups), industry, len(bars_map), len(y), auc)

    if gx:
        X = np.vstack(gx)
        y = np.concatenate(gy)
        d = np.concatenate(gd)
        if len(y) > GLOBAL_SAMPLE:
            idx = rng.choice(len(y), GLOBAL_SAMPLE, replace=False)
            X, y, d = X[idx], y[idx], d[idx]
        fname, auc, prob_q, valid_from = train_one(X, y, d, out_dir, "global")
        if fname:
            entries.append({"id": "global", "industry": "全市场兜底",
                            "codes": [], "samples": len(y), "file": fname,
                            "auc": None if not np.isfinite(auc) else round(auc, 4),
                            "prob_q": prob_q, "valid_from": valid_from,
                            "peer": {}})
            log.info("[兜底] 样本 %d AUC %.4f", len(y), auc)

    index = {
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market": "HK", "kind": kind, "native": cfg["native"],
        "horizon": horizon, "features": list(feat.FEATURE_NAMES),
        "data_last": data_last or None,
        "models": entries,
    }
    tmp = os.path.join(out_dir, "index.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(out_dir, "index.json"))
    keep = {e["file"] for e in entries}
    for name in os.listdir(out_dir):
        if name.endswith(".txt") and name not in keep:
            os.remove(os.path.join(out_dir, name))
    log.info("%s 完成：%d 个模型，data_last=%s", kind, len(entries),
             index["data_last"])
    return index


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="港股 LightGBM 训练（日线+分时）")
    ap.add_argument("--timeframe", choices=("daily", "intraday", "all"),
                    default="all")
    ap.add_argument("--min-symbols", type=int, default=3)
    ap.add_argument("--only", help="只训一个行业（调试）")
    ap.add_argument("--limit", type=int, default=0, help="每行业最多 N 只（调试）")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    t0 = time.time()
    ind_map = load_industry_map()
    kinds = (("daily", "intraday") if args.timeframe == "all"
             else (args.timeframe,))
    for kind in kinds:
        train_kind(kind, args, ind_map)
    log.info("全部完成 %.0fs → %s", time.time() - t0, MODELS_ROOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
