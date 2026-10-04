#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包 LightGBM 模型缓存：data/models/hk → data/models_hk.tar.gz。

发布端 build_release.sh 会调用它把模型注入客户端包；
客户端首次使用 lgbm 算法时由 market_sniper.lgbm_model.ensure_cache() 自动解包。

用法：
    python3 scripts/pack_lgbm_cache.py
    python3 scripts/pack_lgbm_cache.py --out dist/lgbm_hk_models.tar.gz
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tarfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("market_sniper.pack_lgbm")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(_ROOT, "data", "models", "hk")
DEFAULT_OUT = os.path.join(_ROOT, "data", "models_hk.tar.gz")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打包 LightGBM 模型缓存")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    for kind in ("daily", "intraday"):
        if not os.path.exists(os.path.join(SRC, kind, "index.json")):
            log.error("缺少模型 %s/%s/index.json，先跑 scripts/train_hk_lgbm.py",
                      SRC, kind)
            return 1
    n = sum(len(files) for _, _, files in os.walk(SRC))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    tmp = args.out + ".tmp"
    with tarfile.open(tmp, "w:gz") as tf:
        tf.add(SRC, arcname="hk")
    os.replace(tmp, args.out)
    log.info("已打包 %d 个文件 → %s（%.1f MB）", n, args.out,
             os.path.getsize(args.out) / 1e6)
    return 0


if __name__ == "__main__":
    sys.exit(main())
