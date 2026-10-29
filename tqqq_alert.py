"""
TQQQ 낙폭 분할매수 / 익절 텔레그램 알림 봇 (GitHub Actions용)

전략 (config.json 에서 변경)
  - 사상 최고가 대비 -DROP1 → 1차 매수 (자금의 SPLIT)
  - 같은 최고점 대비 -DROP2 → 2차 매수 (남은 자금 전부)
  - 평균단가 대비 +TP → 전량 익절
  - 백테스트(tqqq_split_backtest.py)와 같은 규칙으로 5분봉을 순서대로 처리

실행
  python tqqq_alert.py            # 새 5분봉 확인 → 신호 있으면 텔레그램 전송
  python tqqq_alert.py --status   # 현재 상태 보고서 전송
  python tqqq_alert.py --test     # 텔레그램 연결 테스트

환경변수 (GitHub Secrets)
  TELEGRAM_TOKEN, CHAT_ID (또는 TELEGRAM_CHAT_ID)   (필수 — 기존 MFI 봇과 같은 시크릿 사용)
  ALPACA_KEY, ALPACA_SECRET          (선택: 없으면 yfinance 사용)
  ALPACA_FEED                        (선택: iex(기본, 무료) / sip(유료))
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "tqqq_state.json"
CONFIG_FILE = ROOT / "tqqq_config.json"
KST = timezone(timedelta(hours=9))
SYMBOL = "TQQQ"


# ------------------------------------------------------------------ utils
def load_json(p):
    return json.loads(p.read_text(encoding="utf-8"))


def save_json(p, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def now_utc():
    return datetime.now(timezone.utc)


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_kst(dt):
    return dt.astimezone(KST).strftime("%m/%d %H:%M KST")


def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("CHAT_ID") or "").strip()
    if not token or not chat:
        print("[telegram 미설정] ↓\n" + text)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"},
        timeout=20,
    )
    if not r.ok:
        print("텔레그램 전송 실패:", r.status_code, r.text)
        r.raise_for_status()


# ------------------------------------------------------------------ data
def fetch_bars(since):
    """since 이후의 5분봉 [(ts, open, high, low, close)] — 시간순"""
    if os.environ.get("ALPACA_KEY") and os.environ.get("ALPACA_SECRET"):
        return fetch_alpaca(since)
    return fetch_yfinance(since)


def fetch_alpaca(since):
    url = f"https://data.alpaca.markets/v2/stocks/{SYMBOL}/bars"
    headers = {"APCA-API-KEY-ID": os.environ["ALPACA_KEY"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET"]}
    feed = os.environ.get("ALPACA_FEED", "iex")
    params = {
        "timeframe": "5Min",
        "start": (since + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 10000,
        "adjustment": "all",
        "feed": feed,
        "sort": "asc",
    }
    if feed == "sip":  # 무료 계정은 최근 15분 SIP 데이터 불가
        params["end"] = (now_utc() - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    while True:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        js = r.json()
        for b in js.get("bars") or []:
            out.append((parse_ts(b["t"]), b["o"], b["h"], b["l"], b["c"]))
        tok = js.get("next_page_token")
        if not tok:
            break
        params["page_token"] = tok
    return out


def fetch_yfinance(since):
    import yfinance as yf
    days = (now_utc() - since).days
    period = "5d" if days < 4 else ("1mo" if days < 25 else "60d")
    df = yf.Ticker(SYMBOL).history(period=period, interval="5m", prepost=True, auto_adjust=False)
    out = []
    for ts, row in df.iterrows():
        t = ts.to_pydatetime().astimezone(timezone.utc)
        if t > since:
            out.append((t, float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])))
    return out


# ------------------------------------------------------------------ strategy engine
def levels(state, cfg):
    peak = state["peak"]
    ref = state.get("entry_peak") or peak  # 보유 중엔 1차 매수 시점의 최고점 기준
    return {
        "buy1": peak * (1 - cfg["drop1"]),
        "buy2": ref * (1 - cfg["drop2"]),
        "buy2_if_flat": peak * (1 - cfg["drop2"]),
        "tp": state["avg"] * (1 + cfg["tp"]) if state.get("avg") else None,
    }


def recompute_avg(state, cfg):
    s = cfg["split"]
    if state["phase"] == "first":
        state["avg"] = state["px1"]
    elif state["phase"] == "full":
        state["avg"] = 1.0 / (s / state["px1"] + (1 - s) / state["px2"])  # 금액가중 평균단가
    else:
        state["avg"] = None


def process_bars(state, bars, cfg):
    """백테스트와 동일한 규칙으로 봉을 처리하고 이벤트 목록을 돌려줌"""
    events = []
    d1, d2, tp, near = cfg["drop1"], cfg["drop2"], cfg["tp"], cfg.get("near_pct", 0)
    for t, o, h, l, c in bars:
        phase = state["phase"]
        bought_this_bar = False

        if phase == "flat":
            lvl1 = state["peak"] * (1 - d1)
            if l <= lvl1:
                state.update(phase="first", px1=min(lvl1, o), entry_peak=state["peak"],
                             buy1_time=t.isoformat(), px2=None, buy2_time=None, near_flags=[])
                recompute_avg(state, cfg)
                events.append(("buy1", t, state["px1"], c))
                bought_this_bar = True
                lvl2 = state["entry_peak"] * (1 - d2)
                if l <= lvl2:  # 같은 봉에 2차까지 (폭락)
                    state.update(phase="full", px2=min(lvl2, o), buy2_time=t.isoformat())
                    recompute_avg(state, cfg)
                    events.append(("buy2", t, state["px2"], c))
            elif near and l <= lvl1 * (1 + near) and "buy1" not in state["near_flags"]:
                state["near_flags"].append("buy1")
                events.append(("near_buy1", t, lvl1, c))
            elif "buy1" in state["near_flags"] and l > lvl1 * (1 + 2 * near):
                state["near_flags"].remove("buy1")  # 충분히 멀어지면 다음 접근 때 다시 알림

        elif not bought_this_bar:
            lvl2 = state["entry_peak"] * (1 - d2)
            tgt = state["avg"] * (1 + tp)
            if phase == "first" and l <= lvl2:  # 2차 매수 우선 (같은 봉이면)
                state.update(phase="full", px2=min(lvl2, o), buy2_time=t.isoformat())
                recompute_avg(state, cfg)
                events.append(("buy2", t, state["px2"], c))
            elif h >= tgt:
                px = max(tgt, o)
                events.append(("sell", t, px, c, state["avg"]))
                state.update(phase="flat", px1=None, px2=None, avg=None, entry_peak=None,
                             buy1_time=None, buy2_time=None, near_flags=[], last_sell=t.isoformat())
            else:
                if near and phase == "first" and l <= lvl2 * (1 + near) and "buy2" not in state["near_flags"]:
                    state["near_flags"].append("buy2"); events.append(("near_buy2", t, lvl2, c))
                if near and h >= tgt * (1 - near) and "tp" not in state["near_flags"]:
                    state["near_flags"].append("tp"); events.append(("near_tp", t, tgt, c))
                if "tp" in state["near_flags"] and h < tgt * (1 - 2 * near):
                    state["near_flags"].remove("tp")
                if "buy2" in state["near_flags"] and l > lvl2 * (1 + 2 * near):
                    state["near_flags"].remove("buy2")

        state["peak"] = max(state["peak"], h)  # 사상 최고가는 항상 갱신
        state["last_price"], state["last_bar"] = c, t.isoformat()
    return events


# ------------------------------------------------------------------ messages
def msg_event(ev, state, cfg):
    kind, t = ev[0], ev[1]
    s = int(cfg["split"] * 100)
    when = fmt_kst(t)
    if kind == "buy1":
        return (f"🟡 <b>TQQQ 1차 매수 신호</b>\n"
                f"최고점 ${state['entry_peak']:.2f} 대비 -{cfg['drop1']*100:.0f}% 도달\n"
                f"👉 자금의 <b>{s}%</b> 매수 (기준가 ${ev[2]:.2f}, 현재가 ${ev[3]:.2f})\n"
                f"다음: 2차 매수 ${state['entry_peak']*(1-cfg['drop2']):.2f} / 익절 ${state['avg']*(1+cfg['tp']):.2f}\n{when}")
    if kind == "buy2":
        return (f"🟠 <b>TQQQ 2차 매수 신호</b>\n"
                f"최고점 ${state['entry_peak']:.2f} 대비 -{cfg['drop2']*100:.0f}% 도달\n"
                f"👉 남은 자금 <b>{100-s}%</b> 전부 매수 (기준가 ${ev[2]:.2f}, 현재가 ${ev[3]:.2f})\n"
                f"평균단가 ${state['avg']:.2f} → 익절 목표 ${state['avg']*(1+cfg['tp']):.2f}\n{when}")
    if kind == "sell":
        return (f"🟢 <b>TQQQ 전량 익절 신호</b>\n"
                f"평균단가 ${ev[4]:.2f} 대비 +{cfg['tp']*100:.0f}% 도달\n"
                f"👉 <b>전량 매도</b> (기준가 ${ev[2]:.2f}, 현재가 ${ev[3]:.2f})\n"
                f"다음 1차 매수: 최고점 ${state['peak']:.2f} 대비 -{cfg['drop1']*100:.0f}% = ${state['peak']*(1-cfg['drop1']):.2f}\n{when}")
    labels = {"near_buy1": "1차 매수가", "near_buy2": "2차 매수가", "near_tp": "익절가"}
    return (f"🔔 TQQQ {labels[kind]} {cfg['near_pct']*100:.0f}% 이내 접근\n"
            f"목표 ${ev[2]:.2f} / 현재 ${ev[3]:.2f}\n{when}")


def msg_status(state, cfg):
    p = state.get("last_price") or 0
    pk = state["peak"]
    lines = [f"📊 <b>TQQQ 전략 상태</b> ({fmt_kst(now_utc())})",
             f"현재가 ${p:.2f} | 최고점 ${pk:.2f} ({(p/pk-1)*100:+.1f}%)"]
    s = int(cfg["split"] * 100)
    if state["phase"] == "flat":
        b1, b2 = pk * (1 - cfg["drop1"]), pk * (1 - cfg["drop2"])
        lines += ["상태: 💵 현금 대기",
                  f"1차 매수({s}%): ${b1:.2f} (현재가 대비 {(b1/p-1)*100:+.1f}%)",
                  f"2차 매수({100-s}%): ${b2:.2f} (현재가 대비 {(b2/p-1)*100:+.1f}%)"]
    else:
        tgt = state["avg"] * (1 + cfg["tp"])
        lines.append(f"상태: 📈 보유 중 ({'1차만' if state['phase']=='first' else '1차+2차'})")
        lines.append(f"1차 ${state['px1']:.2f}" + (f" / 2차 ${state['px2']:.2f}" if state.get("px2") else ""))
        lines.append(f"평균단가 ${state['avg']:.2f} | 평가 {(p/state['avg']-1)*100:+.1f}%")
        if state["phase"] == "first":
            b2 = state["entry_peak"] * (1 - cfg["drop2"])
            lines.append(f"2차 매수({100-s}%): ${b2:.2f} (현재가 대비 {(b2/p-1)*100:+.1f}%)")
        lines.append(f"익절 목표: ${tgt:.2f} (현재가 대비 {(tgt/p-1)*100:+.1f}%)")
    lines.append(f"전략: -{cfg['drop1']*100:.0f}% {s}% / -{cfg['drop2']*100:.0f}% {100-s}% / +{cfg['tp']*100:.0f}% 익절")
    return "\n".join(lines)


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()

    cfg = load_json(CONFIG_FILE)
    state = load_json(STATE_FILE)
    state.setdefault("near_flags", [])
    before = json.dumps(state, sort_keys=True)

    if a.test:
        send_telegram("✅ TQQQ 알림 봇 연결 테스트 성공!")
        return

    # 사용자가 state.json 의 px1/px2 를 실제 체결가로 고쳤을 수 있으니 평균단가 재계산
    if state["phase"] != "flat":
        recompute_avg(state, cfg)

    since = parse_ts(state["last_bar"])
    if now_utc() - since > timedelta(days=55):
        since = now_utc() - timedelta(days=55)
    bars = fetch_bars(since)
    print(f"새 5분봉 {len(bars)}개 (since {since.isoformat()})")

    # 데이터 이상(분할/병합 등) 감지: 직전가 대비 ±40% 이상 한 번에 변하면 중단
    if bars and state.get("last_price"):
        jump = bars[0][4] / state["last_price"]
        if jump < 0.6 or jump > 1.67:
            send_telegram(f"⚠️ TQQQ 가격이 ${state['last_price']:.2f} → ${bars[0][4]:.2f} 로 급변. "
                          f"주식 분할/병합일 수 있으니 state.json 의 peak/px 값을 확인해 주세요. (자동 처리 중단)")
            sys.exit(1)

    events = process_bars(state, bars, cfg)
    for ev in events:
        send_telegram(msg_event(ev, state, cfg))

    # 주간 상태 보고 (매주 첫 실행) 또는 --status
    week = datetime.now(KST).strftime("%G-W%V")
    if a.status or (cfg.get("weekly_status", True) and state.get("last_weekly") != week):
        send_telegram(msg_status(state, cfg))
        state["last_weekly"] = week

    # 저장: 중요한 값이 바뀌었거나, 마지막 저장이 하루 넘게 지났을 때만 (커밋 횟수 줄이기)
    important = {k: v for k, v in state.items() if k not in ("last_bar", "last_price", "saved_at")}
    old = {k: v for k, v in json.loads(before).items() if k not in ("last_bar", "last_price", "saved_at")}
    saved_at = parse_ts(json.loads(before).get("saved_at", "2000-01-01T00:00:00+00:00"))
    if important != old or now_utc() - saved_at > timedelta(hours=20):
        state["saved_at"] = now_utc().isoformat()
        save_json(STATE_FILE, state)
        print("state.json 저장")
    else:
        print("변경 없음 (저장 생략)")


if __name__ == "__main__":
    main()
