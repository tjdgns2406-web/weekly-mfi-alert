"""
주봉 하이킨아시 MFI(14) 스크리너 -> 텔레그램 알림 (지난주 대비 상승/하락 기호 표기)
"""

import os
import sys

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

MFI_PERIOD = 14          # MFI 기간
MFI_THRESHOLD = 40       # 이 값 이하만 알림
DATA_PERIOD = "2y"       # 데이터 조회 기간
SEND_WHEN_EMPTY = True   # 조건 만족 종목이 없어도 메시지 전송
# ---------------------------------------------------------------


def convert_to_heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """일반 OHLCV 데이터를 하이킨아시(Heikin-Ashi) OHLCV로 변환"""
    ha_df = df.copy()
    
    ha_df["Close"] = (df["Open"] + df["High"] + df["Low"] + df["Close"]) / 4

    ha_open = [0.0] * len(df)
    ha_open[0] = (df["Open"].iloc[0] + df["Close"].iloc[0]) / 2
    
    for i in range(1, len(df)):
        ha_open[i] = (ha_open[i - 1] + ha_df["Close"].iloc[i - 1]) / 2
        
    ha_df["Open"] = ha_open
    ha_df["High"] = ha_df[["High", "Open", "Close"]].max(axis=1)
    ha_df["Low"] = ha_df[["Low", "Open", "Close"]].min(axis=1)

    return ha_df


def calc_mfi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """MFI(Money Flow Index) 계산"""
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    mf = tp * df["Volume"]
    delta = tp.diff()

    pos = mf.where(delta > 0, 0.0).where(delta.notna())
    neg = mf.where(delta < 0, 0.0).where(delta.notna())

    pos_sum = pos.rolling(period).sum()
    neg_sum = neg.rolling(period).sum()

    ratio = pos_sum / neg_sum
    mfi = 100 - 100 / (1 + ratio)
    mfi = mfi.where(~((pos_sum == 0) & (neg_sum == 0)), 50.0)
    return mfi


def fetch_weekly(ticker: str) -> pd.DataFrame:
    df = yf.Ticker(ticker).history(
        period=DATA_PERIOD, interval="1wk", auto_adjust=False
    )
    if df is None or df.empty:
        raise ValueError("데이터가 비어 있습니다.")

    df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    if len(df) < MFI_PERIOD + 2:
        raise ValueError(f"데이터 부족 ({len(df)}개)")
    return df


def screen(tickers: list[str]) -> tuple[list[dict], list[str], list[str], list[str]]:
    hits: list[dict] = []
    new_entries: list[str] = []
    exited: list[str] = []
    failed: list[str] = []

    for ticker in tickers:
        try:
            raw_df = fetch_weekly(ticker)
            ha_df = convert_to_heikin_ashi(raw_df)
            mfi = calc_mfi(ha_df, MFI_PERIOD)
            
            latest_mfi = float(mfi.iloc[-1])
            prev_mfi = float(mfi.iloc[-2])

            if pd.isna(latest_mfi) or pd.isna(prev_mfi):
                raise ValueError("MFI 계산 결과가 NaN 입니다.")

            if latest_mfi <= MFI_THRESHOLD:
                hits.append(
                    {
                        "ticker": ticker,
                        "mfi": latest_mfi,
                        "prev_mfi": prev_mfi,
                        "date": raw_df.index[-1].strftime("%Y-%m-%d"),
                    }
                )
                if prev_mfi > MFI_THRESHOLD:
                    new_entries.append(ticker)

            elif prev_mfi <= MFI_THRESHOLD and latest_mfi > MFI_THRESHOLD:
                exited.append(ticker)

            print(f"[OK]   {ticker:<6} HA MFI 이번주: {latest_mfi:6.2f} | 지난주: {prev_mfi:6.2f}")

        except Exception as e:
            failed.append(ticker)
            print(f"[FAIL] {ticker:<6} 건너뜀: {e}", file=sys.stderr)

    hits.sort(key=lambda x: x["ticker"])
    new_entries.sort()
    exited.sort()

    return hits, new_entries, exited, failed


def build_message(hits: list[dict], new_entries: list[str], exited: list[str]) -> str:
    if not hits and not exited:
        return f"주봉 HA-MFI({MFI_PERIOD}) {MFI_THRESHOLD} 이하인 종목이 없습니다."

    lines = [f"📉 주봉 HA-MFI({MFI_PERIOD}) ≤ {MFI_THRESHOLD} 종목 ({len(hits)}개)", ""]
    
    # 지난주 대비 MFI 방향성에 따른 기호 표기 (상승: 🔺, 하락: 🔻)
    for h in hits:
        if h['mfi'] > h['prev_mfi']:
            symbol = "🔺"
        elif h['mfi'] < h['prev_mfi']:
            symbol = "🔻"
        else:
            symbol = ""
            
        lines.append(f"• {h['ticker']}: MFI {h['mfi']:.1f} {symbol}".strip())

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
    
    date_str = hits[0]['date'] if hits else "최신"
    lines.append(f"기준 주봉: {date_str}")
    
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url, data={"chat_id": chat_id, "text": text}, timeout=15
    )
    resp.raise_for_status()


def main() -> None:
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()

    if not token or not chat_id:
        print("환경변수 TELEGRAM_TOKEN, CHAT_ID 미설치", file=sys.stderr)
        sys.exit(1)

    hits, new_entries, exited, failed = screen(TICKERS)

    if failed:
        print(f"\n수집 실패 종목: {', '.join(failed)}", file=sys.stderr)

    if not hits and not exited and not SEND_WHEN_EMPTY:
        print("\nMFI 기준 이하 종목이 없어 메시지를 보내지 않습니다.")
        return

    message = build_message(hits, new_entries, exited)
    try:
        send_telegram(token, chat_id, message)
        print("\n텔레그램 전송 완료:\n" + message)
    except Exception as e:
        print(f"\n텔레그램 전송 실패: {e}", file=sys.stderr)
        pass


if __name__ == "__main__":
    main()
