#!/usr/bin/env python3
"""CLI (bản dòng lệnh). Giao diện đồ hoạ: chạy app.py.

  scan       Quét file nhạc mới từ Telegram vào DB
  diff       Đối chiếu với thư viện offline, xuất new_tracks.csv
  download   Tải các bài "mới" (--maybe để tải cả bài "có thể đã có")
"""
from __future__ import annotations

import argparse
import asyncio
import csv

import core


def log(msg: str, frac: float | None = None) -> None:
    print(msg)


def diff(cfg: dict) -> list[core.Result]:
    con = core.db()
    results = core.compute(cfg, con, log)
    out = core.ROOT / "new_tracks.csv"
    rows = [r.as_dict() for r in results if r.match.status != "have"]
    with out.open("w", newline="", encoding="utf-8") as fh:
        if rows:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    for d in rows:
        tag = "MỚI " if d["status"] == "new" else "CÓ THỂ ĐÃ CÓ"
        extra = f"  ~ {d['local']} ({d['score']}) {d['reason']}" if d["local"] else ""
        print(f"[{tag}] {d['artist']} / {d['file_name']}{extra}")
    print(f"Danh sách: {out}")
    return results


async def download(cfg: dict, include_maybe: bool, limit: int | None) -> None:
    results = diff(cfg)
    items = [r.as_dict() for r in results if r.match.status == "new" or (include_maybe and r.match.status == "maybe")]
    items = items[:limit] if limit else items
    tg = core.Telegram(cfg)
    if not await tg.authorized():
        raise SystemExit("Chưa đăng nhập Telegram. Mở app.py để đăng nhập trước.")
    await tg.download(core.db(), items, log)
    await tg.disconnect()


async def scan(cfg: dict) -> None:
    tg = core.Telegram(cfg)
    if not await tg.authorized():
        raise SystemExit("Chưa đăng nhập Telegram. Mở app.py để đăng nhập trước.")
    await tg.scan(core.db(), log)
    await tg.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    sub.add_parser("diff")
    p = sub.add_parser("download")
    p.add_argument("--maybe", action="store_true")
    p.add_argument("--limit", type=int)
    a = ap.parse_args()
    cfg = core.load_raw_config()
    if a.cmd == "scan":
        asyncio.run(scan(cfg))
    elif a.cmd == "diff":
        diff(cfg)
    else:
        asyncio.run(download(cfg, a.maybe, a.limit))


if __name__ == "__main__":
    main()
