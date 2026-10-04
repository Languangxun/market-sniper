#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载 Stooq 日K整包（自动过 JS PoW + OCR 图形验证码）。

用法：
    python3 scripts/download_stooq.py                 # 下载 hk + us 日K到 data/raw/
    python3 scripts/download_stooq.py --only hk5,us5  # 只下 5 分K
    python3 scripts/download_stooq.py --force         # 已存在也重下
    python3 scripts/download_stooq.py --proxy http://127.0.0.1:7890

下完用 scripts/import_stooq.py 导入。
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
import time
import zipfile

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("market_sniper.download_stooq")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(_ROOT, "data", "raw")

PACKAGES = {
    "hk": ("d_hk_txt", "d_hk_txt.zip"),     # 港股日K ~105MB
    "us": ("d_us_txt", "d_us_txt.zip"),     # 美股日K ~515MB
    "hk5": ("5_hk_txt", "5_hk_txt.zip"),    # 港股5分K ~60MB
    "us5": ("5_us_txt", "5_us_txt.zip"),    # 美股5分K ~681MB
}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _session(proxy: str | None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _UA})
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    else:
        s.trust_env = False           # stooq 直连即可，不走全局代理
    return s


def solve_pow(s: requests.Session) -> bool:
    """站点 JS 工作量证明：找 n 使 sha256(c+n) 以 d 个 0 开头。"""
    r = s.get("https://stooq.com/db/h/", timeout=30)
    m = re.search(r'const c="([^"]+)",d=(\d+)', r.text)
    if not m:
        return True                   # 没触发挑战，直接可用
    c, d = m.group(1), int(m.group(2))
    target = "0" * d
    n = 0
    while not hashlib.sha256(f"{c}{n}".encode()).hexdigest().startswith(target):
        n += 1
    r = s.post("https://stooq.com/__verify", data={"c": c, "n": n}, timeout=30)
    log.info("PoW 已通过（n=%d）", n)
    return r.status_code == 200


def solve_captcha(s: requests.Session, ocr, attempts: int = 8) -> bool:
    for i in range(attempts):
        img = s.get(f"https://stooq.com/q/l/s/i/?{int(time.time()*1000)}&h=l",
                    timeout=30)
        code = ocr.classification(img.content)
        ok = s.get(f"https://stooq.com/q/l/s/?t={code}", timeout=30).text.strip()
        log.info("验证码 OCR=%r -> %s", code, ok)
        if ok == "1":
            return True
        time.sleep(0.5)
    return False


def download(s: requests.Session, package: str, dest: str,
             ocr, attempts: int = 4) -> bool:
    url = f"https://stooq.com/db/d/?b={package}"
    for i in range(attempts):
        if not solve_captcha(s, ocr):
            log.warning("验证码连续失败，重试整包（%d/%d）", i + 1, attempts)
            continue
        r = s.get(url, timeout=120, stream=True)
        ctype = r.headers.get("Content-Type", "")
        total = int(r.headers.get("Content-Length") or 0)
        if r.status_code != 200 or "zip" not in ctype:
            body = next(r.iter_content(200), b"")[:100]
            log.warning("下载被拒(%s %s): %s", r.status_code, ctype, body)
            r.close()
            s.get("https://stooq.com/db/h/", timeout=30)
            continue
        tmp = dest + ".part"
        done = 0
        t0 = time.time()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1024 * 256):
                fh.write(chunk)
                done += len(chunk)
                if total and done % (20 * 1024 * 1024) < 1024 * 256:
                    log.info("  %.1f%%  %.1f MB / %.1f MB  (%.1f MB/s)",
                             done * 100 / total, done / 1e6, total / 1e6,
                             done / 1e6 / max(0.1, time.time() - t0))
        if total and done != total:
            log.warning("大小不符：%d != %d，丢弃重试", done, total)
            os.remove(tmp)
            continue
        os.replace(tmp, dest)
        log.info("已保存 %s（%.1f MB，用时 %.1fs）", dest, done / 1e6,
                 time.time() - t0)
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="下载 Stooq 日K整包")
    p.add_argument("--only", help="逗号分隔：hk,us,hk5,us5（缺省 hk,us）")
    p.add_argument("--out-dir", default=DEFAULT_OUT)
    p.add_argument("--proxy", help="代理（缺省直连，stooq 不需要代理）")
    p.add_argument("--force", action="store_true", help="已存在也重下")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    try:
        import ddddocr
    except ImportError:
        log.error("需要 ddddocr（含 onnxruntime）："
                  "python3 -m pip install --break-system-packages ddddocr")
        return 1

    os.makedirs(args.out_dir, exist_ok=True)
    s = _session(args.proxy)
    if not solve_pow(s):
        log.error("JS 工作量证明失败")
        return 1
    ocr = ddddocr.DdddOcr(show_ad=False)

    keys = ([k.strip() for k in args.only.split(",") if k.strip()]
            if args.only else ["hk", "us"])
    bad = [k for k in keys if k not in PACKAGES]
    if bad:
        log.error("未知包：%s（可选 %s）", ",".join(bad), ",".join(PACKAGES))
        return 1
    for key in keys:
        package, filename = PACKAGES[key]
        dest = os.path.join(args.out_dir, filename)
        if os.path.exists(dest) and not args.force:
            log.info("%s 已存在，跳过（--force 重下）", dest)
            continue
        log.info("==== 下载 %s ====", filename)
        if not download(s, package, dest, ocr):
            log.error("%s 下载失败", filename)
            return 1
        try:
            with zipfile.ZipFile(dest) as zf:
                n = len([i for i in zf.infolist() if not i.is_dir()])
            log.info("%s 校验 OK：%d 个文件", filename, n)
        except zipfile.BadZipFile:
            log.error("%s 不是有效 zip，删除", filename)
            os.remove(dest)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
