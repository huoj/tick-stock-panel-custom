# -*- coding: utf-8 -*-
"""取自选股行情快照（腾讯行情，只读）。

交叉验证：A 股收盘价与本地 kline_daily_enriched(11-05) 比对。
ETF 本地表滞后一天，只能靠这个源。
"""
import json
import sys

import requests

CODES = {
    "159529.SZ": "sz159529",
    "159682.SZ": "sz159682",
    "159586.SZ": "sz159586",
    "601633.SH": "sh601633",
    "300059.SZ": "sz300059",
    "600779.SH": "sh600779",
    "000333.SZ": "sz000333",
    "000651.SZ": "sz000651",
    "605366.SH": "sh605366",
    "600941.SH": "sh600941",
}

# 本地 11-05 收盘（来自 kline_daily_enriched），用于交叉验证
LOCAL_CLOSE = {
    "601633.SH": 14.31, "300059.SZ": 18.98, "600779.SH": 29.00,
    "000333.SZ": 88.12, "000651.SZ": 38.67, "605366.SH": 7.19,
    "600941.SH": 100.38,
}


def main():
    s = requests.Session()
    s.trust_env = False
    url = "http://qt.gtimg.cn/q=" + ",".join(CODES.values())
    r = s.get(url, timeout=15)
    r.encoding = "gbk"
    out = []
    for line in r.text.strip().split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        code = key.replace("v_", "")
        parts = val.strip('"').split("~")
        if len(parts) < 45:
            out.append({"code": code, "error": "short", "raw": parts[:8]})
            continue
        sym = [k for k, v in CODES.items() if v == code]
        sym = sym[0] if sym else code
        rec = {
            "symbol": sym,
            "name": parts[1],
            "close": float(parts[3]),
            "prev_close": float(parts[4]),
            "open": float(parts[5]),
            "volume_shou": parts[6],
            "quote_time": parts[30],
            "change": float(parts[31]),
            "change_pct": float(parts[32]),
            "high": float(parts[33]),
            "low": float(parts[34]),
            "amount_wan": parts[37],
            "turnover_pct": parts[38],
            "amplitude_pct": parts[43] if len(parts) > 43 else None,
        }
        lc = LOCAL_CLOSE.get(sym)
        if lc is not None:
            rec["local_close"] = lc
            rec["match"] = abs(lc - rec["close"]) < 0.005
        out.append(rec)

    # 指数
    idx_url = "http://qt.gtimg.cn/q=sh000001,sz399001,sz399006,sh000300,sh000688,sz399905"
    try:
        r2 = s.get(idx_url, timeout=15)
        r2.encoding = "gbk"
        idx = []
        for line in r2.text.strip().split(";"):
            line = line.strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            p = v.strip('"').split("~")
            if len(p) < 35:
                continue
            idx.append({"code": k.replace("v_", ""), "name": p[1], "close": p[3],
                        "chg_pct": p[32], "amount_wan": p[37] if len(p) > 37 else None})
    except Exception as e:
        idx = [{"error": str(e)}]

    print(json.dumps({"quotes": out, "indices": idx}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
