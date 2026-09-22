import os
import json
import time
import requests
import yfinance as yf
import pandas as pd

# ----------------------------------------------------
# 1. 관심 종목 전체 리스트 (총 138개)
# ----------------------------------------------------
TICKERS = [
    "AAPL", "ABNB", "ACM", "ADBE", "ALAB", "AMAT", "AMD", "AMPH",
    "AMZN", "ANET", "AOOI", "AVAV", "AVGO", "AXON", "AXP", "BA",
    "BABA", "BAC", "BBAI", "BE", "BEAM", "BIDU", "BMY", "BOX",
    "BRK.B", "CARR", "CCJ", "CEG", "CGNX", "CIFR", "CIR", "CLS",
    "CLSK", "COHR", "COIN", "COST", "CRCL", "CRDO", "CRSP", "CRWV",
    "CVX", "ELF", "EMR", "ENTG", "F", "FCX", "FIS", "FLR",
    "FRO", "GD", "GLW", "GOOG", "GOOGL", "HALO", "HD", "HIMS",
    "HUT", "ILMN", "INOD", "INTC", "INTU", "IONQ", "IREN", "JCI",
    "JNJ", "JOBY", "JPM", "KO", "KTOS", "LNG", "LUNR", "MCD",
    "MDB", "META", "MRNA", "MSFT", "MSI", "MSTR", "MU", "NOW",
    "NVDA", "OKLO", "ORCL", "OXY", "PANW", "PATH", "PEP", "PFE",
    "PHM", "PL", "PLTR", "PM", "QCOM", "QLD", "QQQ", "QUBT",
    "SMCI", "SMMT", "SNDK", "SNOW", "SNPS", "SO", "SOFI", "SONY",
    "SOXX", "SOUND", "SPCX", "SPOT", "STRL", "STX", "SYM", "TCTM",
    "TEM", "TER", "TFC", "TM", "TME", "TOL", "TQQQ", "TSLA",
    "TSEM", "TT", "TXN", "U", "UBER", "ULTA", "UNH", "UPST",
    "UTHR", "VKTX", "VLO", "VRT", "VST", "WDC", "WM", "WMT",
    "WULF", "XOM"
]

HISTORY_FILE = "previous_mfi.json"

# ----------------------------------------------------
# 2. MFI(Money Flow Index) 계산 함수
# ----------------------------------------------------
def calculate_mfi(df, period=14):
    try:
        if df is None or df.empty or len(df) < period + 1:
            return None
        
        high = df['High']
        low = df['Low']
        close = df['Close']
        volume = df['Volume']

        typical_price = (high + low + close) / 3
        raw_money_flow = typical_price * volume
        
        positive_flow = [0.0] * len(df)
        negative_flow = [0.0] * len(df)
        
        tp_values = typical_price.values
        rmf_values = raw_money_flow.values
        
        for i in range(1, len(df)):
            if tp_values[i] > tp_values[i - 1]:
                positive_flow[i] = rmf_values[i]
            elif tp_values[i] < tp_values[i - 1]:
                negative_flow[i] = rmf_values[i]
                
        pos_mf = pd.Series(positive_flow, index=df.index).rolling(window=period).sum()
        neg_mf = pd.Series(negative_flow, index=df.index).rolling(window=period).sum()
        
        mfi_ratio = pos_mf / neg_mf
        mfi = 100 - (100 / (1 + mfi_ratio))
        
        val = mfi.iloc[-1]
        return float(val) if not pd.isna(val) else None
    except Exception:
        return None

# ----------------------------------------------------
# 3. 히스토리 데이터 로드 및 저장
# ----------------------------------------------------
def load_previous_data():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("under_40", [])
        except Exception:
            return []
    return []

def save_current_data(under_40_tickers):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump({"under_40": under_40_tickers}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# ----------------------------------------------------
# 4. 텔레그램 메시지 전송 (에러 상세 출력 적용)
# ----------------------------------------------------
def send_telegram_message(message):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    
    if not bot_token or not chat_id:
        print("[ERROR] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 환경변수가 설정되지 않았습니다.")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    
    # parse_mode를 제거하여 마크다운 파싱 오류 전면 차단
    payload = {
        "chat_id": chat_id,
        "text": message
    }
    
    try:
        res = requests.post(url, json=payload, timeout=10)
        print(f"텔레그램 응답 상태 코드: {res.status_code}")
        
        if res.status_code != 200:
            print(f"[텔레그램 전송 실패 상세 이유]: {res.text}")
            # 전송 실패 시 빌드도 에러로 처리하여 알 수 있게 함
            raise Exception(f"Telegram API Error: {res.text}")
        else:
            print("텔레그램 메시지 전송 성공!")
            
    except Exception as e:
        print(f"[텔레그램 예외 발생]: {e}")
        raise e

# ----------------------------------------------------
# 5. 메인 실행 로직
# ----------------------------------------------------
def main():
    mfi_under_40 = []
    mfi_under_30 = []
    mfi_under_20 = []
    mfi_under_10 = []
    
    current_all_under_40 = []

    print("주가 및 MFI 데이터 분석 시작...")
    for ticker in TICKERS:
        try:
            stock = yf.Ticker(ticker)
            df = stock.history(period="1y", interval="1wk")
            
            mfi_val = calculate_mfi(df)
            if mfi_val is not None:
                if mfi_val <= 10:
                    mfi_under_10.append(ticker)
                    current_all_under_40.append(ticker)
                elif mfi_val <= 20:
                    mfi_under_20.append(ticker)
                    current_all_under_40.append(ticker)
                elif mfi_val <= 30:
                    mfi_under_30.append(ticker)
                    current_all_under_40.append(ticker)
                elif mfi_val <= 40:
                    mfi_under_40.append(ticker)
                    current_all_under_40.append(ticker)

        except Exception as e:
            pass
        
        time.sleep(0.05)

    print("데이터 처리 완료. 메시지 생성 중...")

    prev_under_40 = load_previous_data()

    entered_tickers = sorted(list(set(current_all_under_40) - set(prev_under_40)))
    exited_tickers = sorted(list(set(prev_under_40) - set(current_all_under_40)))

    str_40 = ", ".join(mfi_under_40) if mfi_under_40 else "없음"
    str_30 = ", ".join(mfi_under_30) if mfi_under_30 else "없음"
    str_20 = ", ".join(mfi_under_20) if mfi_under_20 else "없음"
    str_10 = ", ".join(mfi_under_10) if mfi_under_10 else "없음"

    str_entered = ", ".join(entered_tickers) if entered_tickers else "없음"
    str_exited = ", ".join(exited_tickers) if exited_tickers else "없음"

    message = f"""📊 [주간 MFI 지표 알림]

🟢 MFI 40 이하
{str_40}

🟡 MFI 30 이하
{str_30}

🟠 MFI 20 이하
{str_20}

🔴 MFI 10 이하
{str_10}

━━━━━━━━━━━━━━━━━━
🆕 이번 주 신규 진입 (40 이하)
{str_entered}

🚪 이번 주 목록 이탈 (40 초과)
{str_exited}
"""

    send_telegram_message(message)
    save_current_data(current_all_under_40)

if __name__ == "__main__":
    main()
