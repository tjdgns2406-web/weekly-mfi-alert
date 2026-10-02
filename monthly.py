"""
하이킨아시(HA) 월봉 MFI 알림 -> 텔레그램
- 마감 확정된 직전 월봉이 (1) HA-MFI <= 40 이고 (2) HA 양봉(HA종가 > HA시가) 이면 알림
- 종목/이름/MFI 계산/마감 확정 규칙은 main.py 것을 그대로 가져다 씀 (main.py는 수정 안 함)
- 매일 실행되지만, 같은 달 알림은 monthly_state.json 기록으로 딱 한 번만 전송
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

# main.py에서 그대로 재사용 (main.py는 import해도 자동 실행되지 않음)
from main import (
    ALT_MFI_PERIOD, DATA_PERIOD, DEFAULT_MFI_PERIOD, MFI_11_TICKERS, TICKERS,
    calc_mfi, convert_to_heikin_ashi, is_kr, label, market_cutoff,
    resample_ohlcv, send_telegram, yf_symbol,
)

MFI_THRESHOLD = 40
STATE_FILE = "monthly_state.json"


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(month_str: str) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_sent_month": month_str,
                   "sent_at_utc": datetime.now(timezone.utc).isoformat()},
                  f, ensure_ascii=False, indent=2)


def download(symbols: list):
    return yf.download(symbols, period=DATA_PERIOD, interval="1d",
                       group_by="ticker", auto_adjust=False,
                       threads=True, progress=False)


def get_daily(data, sym: str) -> pd.DataFrame:
    raw = data[sym][["Open", "High", "Low", "Close", "Volume"]]
    daily = raw.dropna(subset=["Close"]).copy()
    daily.index = pd.to_datetime(daily.index).tz_localize(None).normalize()
    return daily


def evaluate_monthly(daily: pd.DataFrame, t: str, now_utc, target_end: pd.Timestamp):
    """
    반환: dict(결과) 또는 'short'(월봉 데이터 부족)
    조회/데이터 문제는 예외(ValueError)로 던짐 -> 실패 목록에 들어감
    """
    cutoff = market_cutoff(t, now_utc)          # 장중 봉 제외 (main.py와 동일)
    if cutoff <= target_end:
        raise ValueError("해당 월이 아직 마감 확정 전")
    df = daily[(daily.index < cutoff) & (daily.index <= target_end)]
    if df.empty:
        return "short"                           # 대상 월 이전 데이터 없음(신규 상장)

    monthly = resample_ohlcv(df, "ME")
    if monthly.empty:
        return "short"
    if monthly.index[-1].to_period("M") != target_end.to_period("M"):
        raise ValueError("대상 월 봉 없음")

    period = ALT_MFI_PERIOD if t in MFI_11_TICKERS else DEFAULT_MFI_PERIOD
    if len(monthly) < period + 1:
        return "short"

    ha = convert_to_heikin_ashi(monthly)
    mfi = calc_mfi(ha, period).iloc[-1]
    if pd.isna(mfi):
        return "short"

    last = ha.iloc[-1]
    bullish = float(last["Close"]) > float(last["Open"])
    return {"mfi": float(mfi), "bullish": bullish, "p": period,
            "ok": float(mfi) <= MFI_THRESHOLD and bullish}


def build_section(market, hits, shorts, failed, total) -> str:
    if hits:
        lines = [f"■ {market} - {len(hits)}개"]
        for h in hits:
            p = f" (MFI{h['p']})" if h["p"] == ALT_MFI_PERIOD else ""
            lines.append(f"- {label(h['ticker'])}: 월봉 MFI {h['mfi']:.1f}{p}")
        text = "\n".join(lines)
    else:
        text = f"■ {market}\n충족 종목이 없습니다."
    if shorts:
        text += f"\nℹ️ 월봉 데이터 부족 제외: {', '.join(label(x) for x in shorts)}"
    if failed:
        text += f"\n⚠️ 데이터 조회 실패: {', '.join(label(x) for x in failed)}"
    return text


def main() -> None:
    # 월봉 전용 시크릿 (기존 알림과 분리). 봇 토큰은 비어 있으면 기존 봇을 같이 쓰되,
    # 채팅방(CHAT_ID)은 반드시 월봉 전용을 따로 지정해야 함 (섞여서 가는 것 방지)
    token = (os.environ.get("MONTHLY_TELEGRAM_TOKEN", "").strip()
             or os.environ.get("TELEGRAM_TOKEN", "").strip())
    chat_id = os.environ.get("MONTHLY_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("환경변수 MONTHLY_CHAT_ID(및 봇 토큰) 미설정", file=sys.stderr)
        sys.exit(1)

    manual = os.environ.get("EVENT_NAME", "") == "workflow_dispatch"

    now_utc = datetime.now(timezone.utc)
    kst_today = now_utc.astimezone(ZoneInfo("Asia/Seoul")).date()
    # 월 마감 다음 날(1일) 알림 + 실패 대비 재시도(2~3일). 그 외 날짜는 자동 실행 시 조용히 종료
    if not manual and kst_today.day > 3:
        print(f"{kst_today} 은(는) 월봉 알림 시기(매월 1~3일)가 아님 -> 종료")
        return
    # 대상 = 한국 날짜 기준 '직전 달' (매일 08:00 KST 실행 시 미국/한국 모두 마감 확정된 상태)
    target_end = pd.Timestamp(kst_today.replace(day=1) - timedelta(days=1))
    month_str = target_end.strftime("%Y-%m")

    state = load_state()
    if not manual and state.get("last_sent_month") == month_str:
        print(f"{month_str} 월봉 알림은 이미 전송됨 -> 종료")
        return

    symbols = {t: yf_symbol(t) for t in TICKERS}
    try:
        data = download(list(symbols.values()))
    except Exception as e:
        print(f"[WARN] 전체 다운로드 실패: {e}", file=sys.stderr)
        data = None

    results, failed = {}, []

    def process(t, data_):
        daily = get_daily(data_, symbols[t])
        return evaluate_monthly(daily, t, now_utc, target_end)

    for t in symbols:
        try:
            if data is None:
                raise ValueError("다운로드 실패")
            results[t] = process(t, data)
        except Exception:
            failed.append(t)

    # 실패 종목은 한 번 더 재시도
    if failed:
        retry_list, failed = failed, []
        try:
            data2 = download([symbols[t] for t in retry_list])
        except Exception as e:
            print(f"[WARN] 재시도 다운로드 실패: {e}", file=sys.stderr)
            data2 = None
        for t in retry_list:
            try:
                if data2 is None:
                    raise ValueError("다운로드 실패")
                results[t] = process(t, data2)
            except Exception as e:
                failed.append(t)
                print(f"[FAIL] {t:<10} 건너뜀: {e}", file=sys.stderr)

    if len(failed) == len(symbols):
        print("전 종목 조회 실패 -> 전송/기록 없이 종료(다음 실행에서 재시도)", file=sys.stderr)
        sys.exit(1)

    sections = []
    for market, cond in (("미국장", lambda t: not is_kr(t)), ("국장 (dc형)", is_kr)):
        tk = [t for t in symbols if cond(t)]
        hits = [dict(results[t], ticker=t) for t in tk
                if isinstance(results.get(t), dict) and results[t]["ok"]]
        hits.sort(key=lambda x: label(x["ticker"]))
        shorts = sorted([t for t in tk if results.get(t) == "short"], key=label)
        fails = sorted([t for t in failed if cond(t)], key=label)
        sections.append(build_section(market, hits, shorts, fails, len(tk)))

    title = (f"[📈HA 월봉 MFI 알림]{' 🧪수동 테스트' if manual else ''}\n"
             f"({month_str} 월봉 마감 / HA-MFI<={MFI_THRESHOLD} + HA양봉)")
    message = title + "\n\n" + "\n\n==================\n\n".join(sections)

    try:
        send_telegram(token, chat_id, message)
    except Exception as e:
        print(f"텔레그램 전송 실패: {e}", file=sys.stderr)
        sys.exit(1)     # 기록하지 않으므로 다음 실행에서 재시도됨

    print("텔레그램 전송 완료:\n" + message)
    if not manual:
        save_state(month_str)


if __name__ == "__main__":
    main()
