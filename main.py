"""
주봉 MFI(14) 스크리너 -> 텔레그램 알림
 
- 데이터: yfinance (주봉, interval="1wk")
- 조건: 주봉 MFI(14) <= 30 인 종목만 텔레그램으로 전송
- 환경변수: TELEGRAM_TOKEN, CHAT_ID
 
설치:  pip install yfinance pandas requests
실행:  python main.py
"""
 
import os
import sys
 
import pandas as pd
import requests
import yfinance as yf
 
# ----------------------------- 설정 -----------------------------
TICKERS = [
    "AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "GOOGL",
    "META", "AMD", "QQQ", "TQQQ", "QLD", "SOXX",
]
MFI_PERIOD = 14          # MFI 기간
MFI_THRESHOLD = 30       # 이 값 이하만 알림
DATA_PERIOD = "2y"       # 조회 기간 (주봉 약 104개)
SEND_WHEN_EMPTY = True  # 해당 종목이 없을 때도 "없음" 메시지를 보낼지 여부
# ---------------------------------------------------------------
 
 
def calc_mfi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """MFI(Money Flow Index) 계산.
 
    1) 전형가격(TP) = (고가 + 저가 + 종가) / 3
    2) 자금흐름(MF) = TP * 거래량
    3) TP가 전주보다 오르면 긍정 흐름, 내리면 부정 흐름
    4) MFI = 100 - 100 / (1 + 긍정흐름합 / 부정흐름합)  (최근 period개 합)
    """
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    mf = tp * df["Volume"]
    delta = tp.diff()
 
    # 첫 행(diff가 NaN)은 NaN으로 유지해 정확히 period개의 변화량이 쌓인 뒤부터 계산
    pos = mf.where(delta > 0, 0.0).where(delta.notna())
    neg = mf.where(delta < 0, 0.0).where(delta.notna())
 
    pos_sum = pos.rolling(period).sum()
    neg_sum = neg.rolling(period).sum()
 
    ratio = pos_sum / neg_sum          # neg_sum == 0 이면 inf -> MFI 100
    mfi = 100 - 100 / (1 + ratio)
    mfi = mfi.where(~((pos_sum == 0) & (neg_sum == 0)), 50.0)  # 흐름이 전혀 없으면 중립(50)
    return mfi
 
 
def fetch_weekly(ticker: str) -> pd.DataFrame:
    """yfinance로 주봉 데이터를 받아 정리해서 반환."""
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
    """전 종목의 주봉 MFI를 계산하고, 기준 이하 종목과 실패 종목을 반환."""
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
        except Exception as e:  # 한 종목 실패가 전체를 멈추지 않도록
            failed.append(ticker)
            print(f"[FAIL] {ticker:<6} 건너뜀: {e}", file=sys.stderr)
 
    hits.sort(key=lambda x: x["mfi"])  # MFI 낮은 순
    return hits, failed
 
 
def build_message(hits: list[dict]) -> str:
    if not hits:
        return f"주봉 MFI({MFI_PERIOD}) {MFI_THRESHOLD} 이하 종목이 없습니다."
 
    lines = [f"📉 주봉 MFI({MFI_PERIOD}) ≤ {MFI_THRESHOLD} 종목 ({len(hits)}개)", ""]
    for h in hits:
        lines.append(f"• {h['ticker']}: MFI {h['mfi']:.1f} | 종가 {h['close']:,.2f}")
    lines += ["", f"기준 주봉: {hits[0]['date']}"]
    return "\n".join(lines)
 
 
def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url, data={"chat_id": chat_id, "text": text}, timeout=15
    )
    resp.raise_for_status()
 
 
def main() -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    if not token or not chat_id:
        print(
            "환경변수 TELEGRAM_TOKEN, CHAT_ID 가 설정되어 있지 않습니다.",
            file=sys.stderr,
        )
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
        sys.exit(1)
 
 
if __name__ == "__main__":
    main()
