#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步港股行业分类 + 中文名（东财 F10，datacenter.eastmoney.com）。

输出 data/hk_industry.json：{"00700": {"name": "腾讯控股", "industry": "软件服务"}, ...}
并把已有 stocks 表中的港股名称补齐。

用法：
    python3 scripts/sync_hk_industry.py
    python3 scripts/sync_hk_industry.py --no-names   # 只写 JSON 不动 DB
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_sniper.data import db

log = logging.getLogger("market_sniper.sync_hk_industry")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(_ROOT, "data", "hk_industry.json")
URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"


def fetch_all(page_size: int = 500) -> dict[str, dict]:
    out: dict[str, dict] = {}
    page = 1
    while True:
        params = {
            "reportName": "RPT_HKF10_INFO_ORGPROFILE",
            "columns": "SECUCODE,SECURITY_CODE,ORG_NAME,BELONG_INDUSTRY",
            "pageNumber": str(page),
            "pageSize": str(page_size),
            "source": "F10",
            "client": "PC",
        }
        r = requests.get(URL, params=params, timeout=30)
        r.raise_for_status()
        res = (r.json().get("result") or {})
        rows = res.get("data") or []
        for row in rows:
            code = str(row.get("SECURITY_CODE") or "").strip().zfill(5)
            if not code:
                continue
            out[code] = {
                "name": (row.get("ORG_NAME") or "").strip(),
                "industry": (row.get("BELONG_INDUSTRY") or "").strip(),
            }
        pages = int(res.get("pages") or 1)
        log.info("第 %d/%d 页，累计 %d 只", page, pages, len(out))
        if page >= pages or not rows:
            break
        page += 1
        time.sleep(0.2)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="同步港股行业分类")
    ap.add_argument("--no-names", action="store_true", help="不更新 stocks 名称")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")

    data = fetch_all()
    if not data:
        log.error("没有拉到数据")
        return 1
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    log.info("行业映射落盘 %s（%d 只）", args.out, len(data))

    if not args.no_names:
        known = {r["code"] for r in db.list_universe("HK", only_listed=False)}
        n = 0
        for code in known & data.keys():
            name = data[code]["name"]
            if name:
                db.upsert_stock("HK", code, name=name)
                n += 1
        log.info("更新 stocks 名称 %d 个", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
