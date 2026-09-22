"""
주봉 MFI(14) 스크리너 -> 텔레그램 알림
"""

import os
import sys

import pandas as pd
import requests
import yfinance as yf

# ----------------------------- 설정 -----------------------------
TICKERS = [
    # 기존 미국 주식/ETF (12개)
    "AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "GOOGL",
    "META", "AMD", "QQQ", "TQQQ", "QLD", "SOXX",

    # 추가 관심 종목 (검증 완료)
    "MU", "MCD", "MRNA", "MSI", "MDB", "BIDU", "VKTX", "BOX", "VLO", "BAC",
    "BRK-B", "VRT", "BA", "AVGO", "BMY", "BE", "VST", "BBAI", "BEAM", "SOUND",
    "CIFR", "SNDK", "SO", "SMMT", "NOW", "CRM", "CLS", "LNG", "CVX", "SONY",
    "SOFI", "SMCI", "SNOW", "STRL", "MSTR", "SPOT", "SNPS", "SYM", "CRCL", "STX",
    "ANET", "AXP", "ALAB", "IREN", "IONQ", "BABA", "GOOG", "APH", "ACM", "AXON",
    "ADBE", "AMAT", "AAOI", "UPST", "EMR", "AVAV", "ABNB", "XOM", "ELF", "ORCL",
    "OKLO", "OXY", "UBER", "ULTA", "WMT", "WDC", "WM", "UTHR", "UNH", "U",
    "PATH", "INOD", "ENTG", "INTC", "INTU", "LUNR", "LLY", "ILMN", "GD", "JPM",
    "JOBY", "JNJ", "JCI", "CCJ", "CARR", "CEG", "CGNX", "GLW", "COST", "COIN",
    "KO", "COHR", "QUBT", "QCOM", "KTOS", "CRDO", "CRSP", "CLSK", "TSEM", "TER",
    "WULF", "TXN", "TME", "TEM", "TM", "TOL", "TT", "TFC", "TCTG", "PLTR",
    "PANW", "PEP", "F", "PHM", "FRO", "FCX", "PL", "FLR", "FIS", "PM",
    "HALO", "HUT", "HD", "PFE", "HIMS", "SPCX", "CRCL"
]
MFI_PERIOD = 14          # MFI 기간
MFI_THRESHOLD = 30       # 이 값 이하만 알림
DATA_PERIOD = "2y"       # 조회 기간
SEND_WHEN_EMPTY = True   # 조건 만족 종목이 없어도 "없음" 메시지 전송
# ---------------------------------------------------------------

def calc_mfi(df: pd.DataFrame, period: int = 14) -> pd.Series:
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

    df = df.dropna(subset=["High", "Low", "Close", "Volume"])
    if len(df) < MFI_PERIOD + 1:
        raise ValueError(f"데이터 부족 ({len(df)}개 < {MFI_PERIOD + 1}개)")
    return df


def screen(tickers: list[str]) -> tuple[list[dict], list[str]]:
    hits: list[dict] = []
    failed: list[str] = []

    for ticker in tickers:
        try:
            df = fetch_weekly(ticker)
            mfi = calc_mfi(df, MFI_PERIOD)
            latest_mfi = float(mfi.iloc[-1])

            if pd.isna(latest_mfi):
                raise ValueError("MFI 계산 결과가 NaN 입니다.")

            print(f"[OK]   {ticker:<6} MFI({MFI_PERIOD}) = {latest_mfi:6.2f}")

            if latest_mfi <= MFI_THRESHOLD:
                hits.append(
                    {
                        "ticker": ticker,
                        "mfi": latest_mfi,
                        "close": float(df["Close"].iloc[-1]),
                        "date": df.index[-1].strftime("%Y-%m-%d"),
                    }
                )
        except Exception as e:
            failed.append(ticker)
            print(f"[FAIL] {ticker:<6} 건너뜀: {e}", file=sys.stderr)

    hits.sort(key=lambda x: x["mfi"])
    return hits, failed


def build_message(hits: list[dict]) -> str:
    if not hits:
        return f"주봉 MFI({MFI_PERIOD}) {MFI_THRESHOLD} 이하 종목이 없습니다."

    lines = [f"📉 주봉 MFI({MFI_PERIOD}) ≤ {MFI_THRESHOLD} 종목 ({len(hits)}개)", ""]
    for h in hits:
        lines.append(f"• {h['ticker']}: MFI {h['mfi']:.1f} | 종가 ${h['close']:,.2f}")
    lines += ["", f"기준 주봉: {hits[0]['date']}"]
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

    hits, failed = screen(TICKERS)

    if failed:
        print(f"\n수집 실패 종목: {', '.join(failed)}", file=sys.stderr)

    if not hits and not SEND_WHEN_EMPTY:
        print("\nMFI 기준 이하 종목이 없어 메시지를 보내지 않습니다.")
        return

    message = build_message(hits)
    try:
        send_telegram(token, chat_id, message)
        print("\n텔레그램 전송 완료:\n" + message)
    except Exception as e:
        print(f"\n텔레그램 전송 실패: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
