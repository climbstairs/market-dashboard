#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
3단계 시장 국면 대시보드 - 데이터 수집 & 진단 엔진.

  1단계 실현된 반응 (Fast)  : 매크로 이벤트일 OHLC 궤적 + 거래량 + 섹터 내부(로테이션)
  2단계 크로스 에셋 (Fast)  : 10Y 금리 / 달러 / 2s10s 곡선 -> '왜' 해석
  3단계 구조적 상태 (Slow)  : 시장 폭(RSP/SPY) / 하이일드 스프레드 / 풋콜  (CNN 폐기)

데이터 소스(모두 무료):
  FRED API : DGS10, T10Y2Y, BAMLH0A0HYM2, DTWEXBGS(달러)
  Stooq    : spy/rsp/sphb/splv/xly/xlp .us  (OHLCV, 키 불필요)
  CBOE     : 총 풋콜 비율 (best-effort; 실패 시 manual.json 또는 생략)
  events.json : 매크로 발표일 캘린더

서프라이즈 태깅 OFF -> 1단계는 호악재가 아닌 '반응의 형태'만 표기.
"""

import os
import io
import csv
import json
import datetime
import urllib.request
import urllib.parse

FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

CONFIG = {
    "hy_low": 2.8, "hy_high": 6.0,        # HY OAS level -> meter
    "hy_widen_alert": 0.20,               # 20d 확대 경보(%p)
    "vol_strong": 1.30,                   # 거래량 동반 기준 (20일 평균 대비)
    "gap_flat": 0.15,                     # 갭 보합 임계(%)
    "rsp_spy_div": -0.5,                  # RSP/SPY 20d 변화 약화 임계(%)
}


# ---------------------------------------------------------------- fetch
def _get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_close_series(ticker, normalize_yield=False, days=400):
    """[(date, close)] for an index ticker (^TNX, DX-Y.NYB ...), same-day updating.
    Returns the daily close series (tuple shape matches fred_series). [] on failure."""
    rng = "1y" if days <= 370 else "2y"
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(ticker) + f"?range={rng}&interval=1d")
    try:
        d = json.loads(_get(url, headers={"User-Agent": UA, "Accept": "application/json"}))
    except Exception as e:  # noqa
        print(f"[yahoo] {ticker} failed: {e}")
        return []
    res = (d.get("chart") or {}).get("result") or []
    if not res:
        return []
    r0 = res[0]
    ts = r0.get("timestamp") or []
    c = (((r0.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    out = []
    for i, t in enumerate(ts):
        try:
            v = c[i]
            if v is None:
                continue
            v = float(v)
            if normalize_yield and v > 20:      # guard ^TNX x10 convention (45.0 -> 4.50)
                v /= 10.0
            out.append((datetime.datetime.utcfromtimestamp(t).date().isoformat(), round(v, 4)))
        except (IndexError, TypeError, ValueError):
            continue
    return out


def fred_series(series_id, days=400):
    if not FRED_KEY:
        print(f"[fred] no key -> skip {series_id}")
        return []
    start = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    url = ("https://api.stlouisfed.org/fred/series/observations"
           f"?series_id={series_id}&api_key={FRED_KEY}&file_type=json"
           f"&observation_start={start}")
    try:
        data = json.loads(_get(url))
        out = []
        for o in data.get("observations", []):
            v = o.get("value", ".")
            if v not in (".", "", None):
                try:
                    out.append((o["date"], float(v)))
                except ValueError:
                    pass
        return out
    except Exception as e:  # noqa
        print(f"[fred] {series_id} failed: {e}")
        return []


def _yahoo_chart(symbol):
    """Yahoo v8 chart JSON (no auth/crumb needed). symbol 'spy.us' -> 'SPY'."""
    yf = symbol.replace(".us", "").upper()
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf}?range=2y&interval=1d"
    d = json.loads(_get(url, headers={"User-Agent": UA, "Accept": "application/json"}))
    res = (d.get("chart") or {}).get("result") or []
    if not res:
        return []
    r0 = res[0]
    ts = r0.get("timestamp") or []
    q = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
    o, h, l, c, v = (q.get(k) or [] for k in ("open", "high", "low", "close", "volume"))
    out = []
    for i, t in enumerate(ts):
        try:
            co = c[i]
            if co is None:
                continue
            out.append({
                "date": datetime.datetime.utcfromtimestamp(t).date().isoformat(),
                "o": float(o[i]) if i < len(o) and o[i] is not None else float(co),
                "h": float(h[i]) if i < len(h) and h[i] is not None else float(co),
                "l": float(l[i]) if i < len(l) and l[i] is not None else float(co),
                "c": float(co),
                "v": float(v[i]) if i < len(v) and v[i] is not None else 0.0,
            })
        except (IndexError, TypeError, ValueError):
            continue
    return out


def _stooq_csv(symbol):
    rows = list(csv.DictReader(io.StringIO(_get(f"https://stooq.com/q/d/l/?s={symbol}&i=d"))))
    out = []
    for r in rows:
        try:
            out.append({"date": r["Date"], "o": float(r["Open"]), "h": float(r["High"]),
                        "l": float(r["Low"]), "c": float(r["Close"]),
                        "v": float(r["Volume"]) if r.get("Volume") not in (None, "", "0") else 0.0})
        except (ValueError, KeyError):
            continue
    return out


def stooq_ohlc(symbol):
    """Daily OHLCV, ascending. Yahoo v8 chart first (GitHub-IP friendly),
    Stooq as fallback. Returns [] only if both fail."""
    try:
        out = _yahoo_chart(symbol)
        if out:
            print(f"[yahoo] {symbol} ok ({len(out)} rows)")
            return out
        print(f"[yahoo] {symbol} empty -> trying stooq")
    except Exception as e:  # noqa
        print(f"[yahoo] {symbol} failed ({e}) -> trying stooq")
    try:
        out = _stooq_csv(symbol)
        print(f"[stooq] {symbol} {'ok '+str(len(out))+' rows' if out else 'empty'}")
        return out
    except Exception as e:  # noqa
        print(f"[stooq] {symbol} failed: {e}")
        return []


def cboe_putcall():
    """Best-effort total put/call ratio. Returns float or None.
    CBOE endpoints change often; falls back to manual.json -> None."""
    candidates = [
        "https://cdn.cboe.com/api/global/us_indices/daily_prices/PCALL.json",
    ]
    for url in candidates:
        try:
            d = json.loads(_get(url))
            if isinstance(d, dict) and "data" in d and d["data"]:
                last = d["data"][-1]
                for k in ("close", "value", "ratio"):
                    if isinstance(last, dict) and k in last:
                        return float(last[k])
        except Exception:  # noqa
            continue
    try:
        with open(os.path.join(HERE, "manual.json"), encoding="utf-8") as f:
            m = json.load(f)
            if m.get("put_call") is not None:
                return float(m["put_call"])
    except Exception:  # noqa
        pass
    print("[cboe] put/call unavailable")
    return None


# ---------------------------------------------------------------- helpers
def find_bar(series, date_str):
    for i, b in enumerate(series):
        if b["date"] == date_str:
            return i, b
    return None, None


def reaction(series, date_str):
    """gap / open->close / day / volume-ratio for an event date, or None."""
    i, b = find_bar(series, date_str)
    if b is None or i == 0:
        return None
    prev_c = series[i - 1]["c"]
    vols = [x["v"] for x in series[max(0, i - 20):i] if x["v"] > 0]
    vavg = sum(vols) / len(vols) if vols else 0
    return {
        "gap_pct": round((b["o"] / prev_c - 1) * 100, 2) if prev_c else None,
        "oc_pct": round((b["c"] / b["o"] - 1) * 100, 2) if b["o"] else None,
        "day_pct": round((b["c"] / prev_c - 1) * 100, 2) if prev_c else None,
        "vol_ratio": round(b["v"] / vavg, 2) if vavg else None,
    }


def pattern(r):
    """Describe reaction shape (no surprise tag)."""
    if not r or r["gap_pct"] is None or r["oc_pct"] is None:
        return "데이터 없음", "a"
    g, oc = r["gap_pct"], r["oc_pct"]
    flat = CONFIG["gap_flat"]
    if g < -flat and oc > 0:
        return "갭다운→양봉 회복", "g"
    if g < -flat and oc <= 0:
        return "갭다운→음봉 (투매 지속)", "r"
    if g > flat and oc < 0:
        return "갭업→음봉 소멸", "r"
    if g > flat and oc >= 0:
        return "갭업→양봉 지속", "g"
    return "보합권 반응", "a"


def chg(series, lag, idx=-1):
    if len(series) <= abs(lag) - idx:
        return None
    try:
        return series[idx][1] - series[idx - lag][1]
    except IndexError:
        return None


# ---------------------------------------------------------------- engine
def build():
    spy = stooq_ohlc("spy.us")
    rsp = stooq_ohlc("rsp.us")
    sphb = stooq_ohlc("sphb.us")
    splv = stooq_ohlc("splv.us")
    xly = stooq_ohlc("xly.us")
    xlp = stooq_ohlc("xlp.us")

    # 금리·달러: 당일 갱신되는 야후 지수 티커 우선 (FRED는 T+1 시차 -> 이벤트 반응 누락).
    # 실패 시 FRED로 폴백. 곡선·하이일드는 시차 무의미하므로 FRED 유지.
    y10 = yahoo_close_series("^TNX", normalize_yield=True) or fred_series("DGS10")
    dxy = yahoo_close_series("DX-Y.NYB") or fred_series("DTWEXBGS")
    s2s10 = fred_series("T10Y2Y")
    hy = fred_series("BAMLH0A0HYM2")

    try:
        with open(os.path.join(HERE, "events.json"), encoding="utf-8") as f:
            events = json.load(f).get("events", [])
    except Exception:  # noqa
        events = []
    putcall = cboe_putcall()

    stooq_ok = bool(spy)
    fred_ok = bool(hy or s2s10)

    # ---- STAGE 1: realized reaction ----
    spy_dates = {b["date"] for b in spy}
    past_events = sorted([e for e in events if e["date"] in spy_dates],
                         key=lambda e: e["date"])
    ledger = []
    for e in past_events[-6:][::-1]:
        r = reaction(spy, e["date"])
        if not r:
            continue
        lbl, cls = pattern(r)
        ledger.append({"date": e["date"][5:], "name": e["name"],
                       "oc_pct": r["oc_pct"], "day_pct": r["day_pct"],
                       "vol_ratio": r["vol_ratio"], "pattern": lbl, "cls": cls})

    if ledger:
        r0 = reaction(spy, past_events[-1]["date"])
        latest_full = {**ledger[0], **r0,
                       "date_full": past_events[-1]["date"],
                       "strong_vol": (r0["vol_ratio"] or 0) >= CONFIG["vol_strong"]}
    else:
        latest_full = None

    recover = sum(1 for x in ledger if "회복" in x["pattern"])
    ledger_summary = (f"최근 {len(ledger)}건 중 {recover}건 '갭다운 후 회복' — 하방 충격 소화 경향"
                      if ledger else "이벤트 데이터 없음")

    def ratio_day(a, b):
        if len(a) < 2 or len(b) < 2:
            return None
        rn = a[-1]["c"] / b[-1]["c"]
        rp = a[-2]["c"] / b[-2]["c"]
        return round((rn / rp - 1) * 100, 2) if rp else None

    sphb_splv = ratio_day(sphb, splv)
    xly_xlp = ratio_day(xly, xlp)
    index_day = round((spy[-1]["c"] / spy[-2]["c"] - 1) * 100, 2) if len(spy) >= 2 else None
    internal_riskoff = (sphb_splv is not None and sphb_splv < -0.4)
    internal_tag = ("지수 대비 내부 risk-off — '둔감'이 아닌 방어적 반응" if internal_riskoff
                    else "내부도 동반 — 표면 반응과 일치")

    stage1 = {
        "latest": latest_full, "ledger": ledger, "summary": ledger_summary,
        "internals": {"sphb_splv": sphb_splv, "xly_xlp": xly_xlp,
                      "index_day": index_day, "riskoff": internal_riskoff,
                      "tag": internal_tag},
    }

    # ---- STAGE 2: cross-asset context ----
    y10_now = y10[-1][1] if y10 else None
    y10_bp = round(chg(y10, 1) * 100, 0) if (y10 and chg(y10, 1) is not None) else None
    dxy_now = dxy[-1][1] if dxy else None
    dxy_pct = (round((dxy[-1][1] / dxy[-2][1] - 1) * 100, 2)
               if len(dxy) >= 2 else None)
    s_now = s2s10[-1][1] if s2s10 else None
    s_prev = s2s10[-2][1] if len(s2s10) >= 2 else None

    idx_up = (index_day or 0) > 0
    rates_up = (y10_bp or 0) > 2
    dollar_up = (dxy_pct or 0) > 0.1
    if idx_up and rates_up and dollar_up:
        ctx = ("주가 회복 + 금리·달러 동반 상승 → '호재를 악재로'(금리 우려) 맥락. "
               "양봉은 안도가 아니라 금리 상승을 견디는 회복으로 해석.")
        ctx_cls = "a"
    elif not idx_up and rates_up:
        ctx = "주가 하락 + 금리 상승 → 긴축/금리 우려 주도 하락."
        ctx_cls = "r"
    elif not idx_up and not rates_up:
        ctx = "주가 하락 + 금리 하락 → 성장 우려형 risk-off (안전자산 선호)."
        ctx_cls = "r"
    elif idx_up and not rates_up and not dollar_up:
        ctx = "주가 상승 + 금리·달러 안정 → 순수 안도(골디락스)에 가까움."
        ctx_cls = "g"
    else:
        ctx = "주가 움직임에 금리·달러가 동조하지 않음 → 매크로보다 수급/차익 요인."
        ctx_cls = "a"

    stage2 = {
        "y10": {"level": round(y10_now, 2) if y10_now else None, "day_bp": y10_bp},
        "dxy": {"level": round(dxy_now, 2) if dxy_now else None, "day_pct": dxy_pct},
        "curve": {"level": round(s_now, 2) if s_now is not None else None,
                  "prev": round(s_prev, 2) if s_prev is not None else None,
                  "inverted": (s_now is not None and s_now < 0)},
        "context": ctx, "context_cls": ctx_cls,
    }

    # ---- STAGE 3: structural state ----
    breadth_20d = None
    if len(rsp) > 21 and len(spy) > 21:
        rn = rsp[-1]["c"] / spy[-1]["c"]
        ro = rsp[-21]["c"] / spy[-21]["c"]
        breadth_20d = round((rn / ro - 1) * 100, 2) if ro else None
    if breadth_20d is None:
        b_state, b_meter, b_cls = "데이터 없음", 50, "a"
    elif breadth_20d < CONFIG["rsp_spy_div"]:
        b_state, b_meter, b_cls = "약화 ↓", 35, "r"
    elif breadth_20d > 0.5:
        b_state, b_meter, b_cls = "광범위 ↑", 75, "g"
    else:
        b_state, b_meter, b_cls = "중립", 55, "a"

    hy_now = hy[-1][1] if hy else None
    hy_20d = chg(hy, 20) if hy else None
    hy_meter = (min(100, max(0, (hy_now - CONFIG["hy_low"]) /
                (CONFIG["hy_high"] - CONFIG["hy_low"]) * 100)) if hy_now else 50)
    hy_alert = (hy_20d or 0) > CONFIG["hy_widen_alert"]

    pc_meter = min(100, max(0, (putcall - 0.6) / (1.3 - 0.6) * 100)) if putcall else 50

    stage3 = {
        "breadth": {"value": breadth_20d, "state": b_state, "meter": b_meter, "cls": b_cls},
        "hy": {"level": round(hy_now, 2) if hy_now else None,
               "chg20d": round(hy_20d, 2) if hy_20d is not None else None,
               "meter": round(hy_meter), "alert": hy_alert},
        "putcall": {"value": round(putcall, 2) if putcall else None,
                    "meter": round(pc_meter)},
    }

    # ---- SYNTHESIS: verdict + conflict ----
    REG = {
        "healthy":     {"label": "호재 민감 장세", "color": "#34d399"},
        "stress":      {"label": "악재 민감 장세", "color": "#f87171"},
        "complacency": {"label": "호재 둔감 (고점 경계)", "color": "#fbbf24"},
        "blind_trend": {"label": "맹목적 추세 추종", "color": "#60a5fa"},
    }
    surface_ok = bool(latest_full and (latest_full.get("oc_pct") or 0) > 0)
    structure_weak = (b_cls == "r") or hy_alert or internal_riskoff
    stress_now = (((y10_bp or 0) > 8 and not idx_up)
                  or (hy_alert and (index_day or 0) < -0.5))

    if stress_now:
        winner = "stress"
    elif surface_ok and structure_weak:
        winner = "blind_trend"
    elif surface_ok and not structure_weak:
        winner = "healthy"
    elif (putcall or 0) < 0.7 and b_cls != "r":
        winner = "complacency"
    else:
        winner = "blind_trend"

    conflict = surface_ok and structure_weak
    strategy = {
        "healthy": "표면·내부가 함께 견조. 주도주 매수, 가벼운 악재는 저가매수 기회.",
        "stress": "현금 비중 확대, 펀더멘탈 확실한 방어주 위주 재편.",
        "complacency": "추가 매수 자제, 분할 매도로 차익 실현.",
        "blind_trend": "표면은 견조하나 내부·신용 경계. 추세 추종하되 헤지 동반 권고.",
    }[winner]

    return {
        "updated": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "verdict": {"regime": winner, "label": REG[winner]["label"],
                    "color": REG[winner]["color"], "strategy": strategy,
                    "conflict": conflict},
        "stage1": stage1, "stage2": stage2, "stage3": stage3,
        "status": {"fred_ok": fred_ok, "stooq_ok": stooq_ok,
                   "putcall_ok": putcall is not None,
                   "surprise_tagging": False},
    }


def update_history(snap):
    path = os.path.join(HERE, "history.json")
    try:
        hist = json.load(open(path, encoding="utf-8"))
    except Exception:  # noqa
        hist = []
    today = datetime.date.today().isoformat()
    hist = [h for h in hist if h.get("date") != today]
    hist.append({"date": today, "regime": snap["verdict"]["regime"],
                 "label": snap["verdict"]["label"],
                 "conflict": snap["verdict"]["conflict"]})
    hist = sorted(hist, key=lambda h: h["date"])[-180:]
    json.dump(hist, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def main():
    snap = build()
    json.dump(snap, open(os.path.join(HERE, "data.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    update_history(snap)
    v = snap["verdict"]
    print(f"[done] {v['label']} (conflict={v['conflict']}) | "
          f"fred={snap['status']['fred_ok']} stooq={snap['status']['stooq_ok']} "
          f"putcall={snap['status']['putcall_ok']}")


if __name__ == "__main__":
    main()
