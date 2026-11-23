# -*- coding: utf-8 -*-
"""查找本地是否有 11-05 的公告/新闻/龙虎榜数据，并列出所有 ext_data 表的新鲜度。只读。"""
import os
import re

import polars as pl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

print("== data 顶层目录 ==")
for x in sorted(os.listdir(DATA)):
    p = os.path.join(DATA, x)
    print("   ", x, "(dir)" if os.path.isdir(p) else "")

print("\n== ext_data 各表最新日期 ==")
ext = os.path.join(DATA, "ext_data")
if os.path.isdir(ext):
    for t in sorted(os.listdir(ext)):
        tp = os.path.join(ext, t)
        if not os.path.isdir(tp):
            continue
        tsdir = os.path.join(tp, "timeseries")
        if os.path.isdir(tsdir):
            ds = sorted([d[5:] for d in os.listdir(tsdir) if d.startswith("date=")])
            print(f"   {t:<28} timeseries  n={len(ds):<5} last={ds[-1] if ds else None}")
        else:
            pp = os.path.join(tp, "part.parquet")
            info = ""
            if os.path.exists(pp):
                try:
                    info = f" rows={pl.read_parquet(pp).height}"
                except Exception as e:
                    info = f" ERR {type(e).__name__}"
            print(f"   {t:<28} snapshot    {info}")

print("\n== user_data 文件 ==")
ud = os.path.join(DATA, "user_data")
if os.path.isdir(ud):
    for f in sorted(os.listdir(ud)):
        fp = os.path.join(ud, f)
        import datetime
        mt = datetime.datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%m-%d %H:%M")
        print(f"   {f:<40} mtime={mt} size={os.path.getsize(fp)}")

print("\n== 搜索含公告/新闻/资讯的表 ==")
for dirpath, dirnames, filenames in os.walk(DATA):
    if "ext_data" in dirpath or "kline" in dirpath:
        continue
    for f in filenames:
        if re.search(r"notice|news|announce|cls|telegraph|info|report", f, re.I):
            print("   ", os.path.relpath(os.path.join(dirpath, f), DATA))
