"""
MFI 3중 조건 스크리너 -> 텔레그램 알림 (하이킨아시 기준)
- 일봉 MFI(14) <= 30
- 주봉 MFI(14, 특정 종목 11) <= 30  (마감된 직전 주봉)
- 월봉 MFI(14, 특정 종목 11) <= 60  (마감된 직전 월봉)
- 세 조건 모두 충족(AND) 시 이름순 알림
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ----------------------------- 설정 -----------------------------
TICKERS = [
    "AAOI", "AAPL", "ABNB", "ACM", "ADBE", "AGQ", "ALAB", "AMAT", "AMD", "AMPH",
    "AMZN", "ANET", "APH", "ARKF", "ASTS", "AVAV", "AVGO", "AXON", "AXP", "AXTI",
    "BA", "BABA", "BAC", "BBAI", "BE", "BEAM", "BIDU", "BITX", "BMY", "BOTZ",
    "BOX", "BRK.B", "BWXT", "CARR", "CCJ", "CEG", "CGNX", "CIFR", "CIR", "CLS",
    "CLSK", "COHR", "COIN", "COST", "CPER", "CRCL", "CRDO", "CRM", "CRSP", "CRWV",
    "DAL", "DDOG", "DE", "DELL", "DFH", "DFEN", "DGRO", "DIS", "DIVB", "DIVO",
    "DRAM", "DVA", "EEM", "ELF", "EMR", "ENTG", "ETU", "EWL", "F", "FAS",
    "FCX", "FIS", "FLR", "FRO", "GD", "GEV", "GLW", "GME", "GOOG", "GOOGL",
    "HALO", "HD", "HIMS", "HOOD", "HUT", "IBM", "IEMG", "IGV", "ILMN", "INOD",
    "INTC", "INTU", "IONQ", "IREN", "IWB", "JCI", "JNJ", "JOBY", "JPM", "KO",
    "KTOS", "LHX", "LLY", "LMT", "LNG", "LRCX", "LULU", "LUNR", "MCD", "MDB",
    "META", "MMM", "MP", "MRNA", "MRVL", "MSFT", "MSI", "MSTR", "MU", "NAIL",
    "NBIS", "NEE", "NFLX", "NKE", "NOK", "NOW", "NRIX", "NTRA", "NU", "NVDA",
    "O", "OKLO", "ORCL", "OXY", "PANW", "PATH", "PEP", "PFE", "PG", "PHM",
    "PL", "PLTR", "PM", "PPA", "QCOM", "QLD", "QQQ", "QBTS", "QUBT", "RCAT",
    "RDDT", "RDW", "RDVY", "RGTI", "RKLB", "ROBO", "SCHD", "SMCI", "SMMT", "SMR",
    "SNDK", "SNOW", "SNPS", "SO", "SOFI", "SONY", "SOUN", "SOXL", "SOXX", "SPCX",
    "SPOT", "STL", "STRL", "STX", "SYM", "T", "TCOM", "TCTG", "TCTM", "TE",
    "TEM", "TER", "TFC", "TGTX", "TM", "TME", "TOL", "TQQQ", "TRIN", "TSLA",
    "TSEM", "TT", "TXN", "U", "UBER", "UCO", "UGL", "ULTA", "UNH", "UPST",
    "UTHR", "VEA", "VIG", "VKTX", "VLO", "VOOG", "VRT", "VST", "VYM", "WDC",
    "WM", "WMT", "WULF", "XLC", "XLE", "XLF", "XLK", "XLY", "XOM"
]

# 주봉/월봉에서 MFI(11)을 쓰는 종목
MFI_11_TICKERS = {
    "VIG", "VYM", "RDVY", "DGRO", "DIVB", "SCHD", "DIVO",
    "SNDK", "MU", "GEV", "AMD", "VLO", "ABNB"
}

USE_HEIKIN_ASHI = True   # False로 바꾸면 일반 캔들로 계산
DEFAULT_MFI_PERIOD = 14
ALT_MFI_PERIOD = 11      # 주봉/월봉 예외 종목만
DAILY_THRESHOLD = 30
WEEKLY_THRESHOLD = 30
MONTHLY_THRESHOLD = 60
DATA_PERIOD = "10y"
SEND_WHEN_EMPTY = True
# ---------------------------------------------------------------


def yf_symbol(t: str) -> str:
    """BRK.B -> BRK-B (야후 표기)"""
    return t.replace(".", "-") if re.fullmatch(r"[A-Z]+\.[A-Z]", t) else t


def convert_to_heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    o, h, l, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    ha_c = (o + h + l + c) / 4
    ha_o = np.empty(len(df))
    ha_o[0] = (o[0] + c[0]) / 2
    for i in range(1, len(df)):
        ha_o[i] = (ha_o[i - 1] + ha_c[i - 1]) / 2
    out = df.copy()
    out["Open"] = ha_o
    out["Close"] = ha_c
    out["High"] = np.maximum.reduce([h, ha_o, ha_c])
    out["Low"] = np.minimum.reduce([l, ha_o, ha_c])
    return out


def calc_mfi(df: pd.DataFrame, period: int) -> pd.Series:
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    mf = tp * df["Volume"]
    delta = tp.diff()

    pos = mf.where(delta > 0, 0.0).where(delta.notna())
    neg = mf.where(delta < 0, 0.0).where(delta.notna())

    pos_sum = pos.rolling(period).sum()
    neg_sum = neg.rolling(period).sum()

    mfi = 100 - 100 / (1 + pos_sum / neg_sum)
    mfi = mfi.where(~((pos_sum == 0) & (neg_sum == 0)), 50.0)
    return mfi


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {"Open": "first", "High": "max", "Low": "min",
           "Close": "last", "Volume": "sum"}
    try:
        out = df.resample(rule).agg(agg)
    except ValueError:  # 구버전 pandas (ME -> M)
        out = df.resample("M").agg(agg)
    return out.dropna(subset=["Close"])


def last_mfi(df: pd.DataFrame, period: int):
    if len(df) < period + 3:
        return None
    if USE_HEIKIN_ASHI:
        df = convert_to_heikin_ashi(df)
    v = calc_mfi(df, period).iloc[-1]
    return None if pd.isna(v) else float(v)


def evaluate(daily: pd.DataFrame, cutoff, alt: bool):
    """cutoff 이전 날짜의 봉만 사용. 주/월봉은 cutoff 전에 마감된 것만."""
    df = daily[daily.index < cutoff]
    if df.empty:
        return None
    p_wm = ALT_MFI_PERIOD if alt else DEFAULT_MFI_PERIOD

    weekly = resample_ohlcv(df, "W-FRI")
    weekly = weekly[weekly.index < cutoff]
    monthly = resample_ohlcv(df, "ME")
    monthly = monthly[monthly.index < cutoff]

    d = last_mfi(df, DEFAULT_MFI_PERIOD)
    w = last_mfi(weekly, p_wm)
    m = last_mfi(monthly, p_wm)
    if None in (d, w, m):
        return None
    ok = d <= DAILY_THRESHOLD and w <= WEEKLY_THRESHOLD and m <= MONTHLY_THRESHOLD
    return {"d": d, "w": w, "m": m, "p": p_wm, "ok": ok,
            "date": df.index[-1].strftime("%Y-%m-%d")}


def screen(data, symbols, today):
    hits, new_entries, exited, failed = [], [], [], []

    for t, sym in symbols.items():
        try:
            raw = data[sym][["Open", "High", "Low", "Close", "Volume"]]
            daily = raw.dropna(subset=["Close"]).copy()
            daily.index = pd.to_datetime(daily.index).tz_localize(None).normalize()
            daily = daily[daily.index < today]
            if len(daily) < 60:
                raise ValueError("데이터 부족")

            alt = t in MFI_11_TICKERS
            now = evaluate(daily, today, alt)
            prev = evaluate(daily, daily.index[-1], alt)  # 전 거래일 기준
            if now is None:
                raise ValueError("MFI 계산 불가")

            prev_ok = bool(prev and prev["ok"])
            if now["ok"]:
                now["ticker"] = t
                now["prev_d"] = prev["d"] if prev else now["d"]
                hits.append(now)
                if not prev_ok:
                    new_entries.append(t)
            elif prev_ok:
                exited.append(t)

            print(f"[OK]   {t:<6} 일 {now['d']:5.1f} | 주 {now['w']:5.1f} | 월 {now['m']:5.1f}")
        except Exception as e:
            failed.append(t)
            print(f"[FAIL] {t:<6} 건너뜀: {e}", file=sys.stderr)

    hits.sort(key=lambda x: x["ticker"])
    new_entries.sort()
    exited.sort()
    return hits, new_entries, exited, failed


def build_message(hits, new_entries, exited, kst_now) -> str:
    title = (f"일봉≤{DAILY_THRESHOLD} / 주봉≤{WEEKLY_THRESHOLD} / "
             f"월봉≤{MONTHLY_THRESHOLD}")
    if not hits and not exited:
        return f"📉 HA-MFI 3중 조건 ({title})\n충족 종목이 없습니다."

    lines = [f"📉 HA-MFI 3중 조건 ({title}) - {len(hits)}개", ""]
    for h in hits:
        if h["d"] > h["prev_d"]:
            symbol = "💹"
        elif h["d"] < h["prev_d"]:
            symbol = "🔻"
        else:
            symbol = ""
        period = f" (주·월 MFI{h['p']})" if h["p"] == ALT_MFI_PERIOD else ""
        lines.append(
            f"• {h['ticker']}: 일 {h['d']:.1f} | 주 {h['w']:.1f} | 월 {h['m']:.1f} {symbol}{period}".strip()
        )

    lines.append("\n----------------------------------")
    if new_entries:
        lines.append(f"🆕 새로 추가된 종목 ({len(new_entries)}개):")
        lines.append("• " + ", ".join(new_entries))
    else:
        lines.append("🆕 새로 추가된 종목: 없음")
    lines.append("")
    if exited:
        lines.append(f"🚪 목록에서 이탈한 종목 ({len(exited)}개):")
        lines.append("• " + ", ".join(exited))
    else:
        lines.append("🚪 목록에서 이탈한 종목: 없음")
    lines.append("----------------------------------")

    date_str = hits[0]["date"] if hits else "최신"
    lines.append(f"기준 일봉: {date_str}")
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # 4096자 제한 대비 분할 전송
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur)
    for c in chunks:
        resp = requests.post(url, data={"chat_id": chat_id, "text": c}, timeout=30)
        resp.raise_for_status()


def main() -> None:
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()
    if not token or not chat_id:
        print("환경변수 TELEGRAM_TOKEN, CHAT_ID 미설정", file=sys.stderr)
        sys.exit(1)

    kst_now = datetime.now(timezone.utc) + timedelta(hours=9)
    today = pd.Timestamp(kst_now.date())

    symbols = {t: yf_symbol(t) for t in TICKERS}
    data = yf.download(
        list(symbols.values()), period=DATA_PERIOD, interval="1d",
        group_by="ticker", auto_adjust=False, threads=True, progress=False,
    )

    hits, new_entries, exited, failed = screen(data, symbols, today)

    if failed:
        print(f"\n수집 실패 종목: {', '.join(failed)}", file=sys.stderr)

    if not hits and not exited and not SEND_WHEN_EMPTY:
        print("\n조건 충족 종목이 없어 메시지를 보내지 않습니다.")
        return

    message = build_message(hits, new_entries, exited, kst_now)
    if failed:
        message += f"\n\n⚠️ 데이터 조회 실패: {', '.join(failed)}"
    try:
        send_telegram(token, chat_id, message)
        print("\n텔레그램 전송 완료:\n" + message)
    except Exception as e:
        print(f"\n텔레그램 전송 실패: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
