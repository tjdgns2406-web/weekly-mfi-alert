"""
TQQQ 낙폭 분할매수 / 익절 텔레그램 알림 봇 (GitHub Actions용)

전략 (config.json 에서 변경)
  - 대기 자금은 SGOV에 파킹
  - 사상 최고가 대비 -DROP1 → SGOV SPLIT 만큼 매도, 신호일(뉴욕) 최저가에 TQQQ 지정가 매수
    (봇이 신호일 장 마감 후 지정가를 알려 줌. 익절가는 기준가 기준으로 고정)
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
from zoneinfo import ZoneInfo  # noqa: E402
ET = ZoneInfo("America/New_York")


def et_date(t):
    return t.astimezone(ET).date().isoformat()
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
                             buy1_time=t.isoformat(), px2=None, buy2_time=None, near_flags=[],
                             limit1_day=et_date(t), limit1=l, limit1_final=False, limit1_filled=False)
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
                events.append(("sell", t, px, c, state["avg"], bool(state.get("limit1_filled"))))
                state.update(phase="flat", px1=None, px2=None, avg=None, entry_peak=None,
                             buy1_time=None, buy2_time=None, near_flags=[], last_sell=t.isoformat(),
                             limit1_day=None, limit1=None, limit1_final=None, limit1_filled=None)
            else:
                if near and phase == "first" and l <= lvl2 * (1 + near) and "buy2" not in state["near_flags"]:
                    state["near_flags"].append("buy2"); events.append(("near_buy2", t, lvl2, c))
                if near and h >= tgt * (1 - near) and "tp" not in state["near_flags"]:
                    state["near_flags"].append("tp"); events.append(("near_tp", t, tgt, c))
                if "tp" in state["near_flags"] and h < tgt * (1 - 2 * near):
                    state["near_flags"].remove("tp")
                if "buy2" in state["near_flags"] and l > lvl2 * (1 + 2 * near):
                    state["near_flags"].remove("buy2")

        # ---- 1차 지정가(신호일 최저가) 추적
        if state["phase"] != "flat" and state.get("limit1_day") and not bought_this_bar:
            if not state.get("limit1_final"):
                if et_date(t) == state["limit1_day"]:
                    state["limit1"] = min(state["limit1"], l)
                else:
                    state["limit1_final"] = True
                    events.append(("limit_set", t, state["limit1"], c))
            if state.get("limit1_final") and not state.get("limit1_filled") and l <= state["limit1"]:
                state["limit1_filled"] = True
                events.append(("limit_fill", t, state["limit1"], c))

        state["peak"] = max(state["peak"], h)  # 사상 최고가는 항상 갱신
        state["last_price"], state["last_bar"] = c, t.isoformat()
    return events


# ------------------------------------------------------------------ messages
def next_monday_kst(t):
    d = t.astimezone(ET).date()
    mon = d + timedelta(days=7 - d.weekday())
    return mon.strftime("%m/%d")


def msg_event(ev, state, cfg):
    kind, t = ev[0], ev[1]
    s = int(cfg["split"] * 100)
    when = fmt_kst(t)
    tp = cfg["tp"] * 100
    if kind == "buy1":
        return (f"🟡 <b>TQQQ 1차 매수 신호</b>\n"
                f"최고점 ${state['entry_peak']:.2f} 대비 -{cfg['drop1']*100:.0f}% 도달 (현재가 ${ev[3]:.2f})\n\n"
                f"👉 할 일\n"
                f"① 오늘 <b>SGOV {s}%</b> 매도 (전략 자금의 {s}%)\n"
                f"② 미국장 마감 후 봇이 알려 주는 <b>오늘 최저가</b>로 TQQQ 지정가 조건주문\n\n"
                f"기준가 ${ev[2]:.2f} → 익절 목표 ${state['avg']*(1+cfg['tp']):.2f} (고정)\n"
                f"2차 매수가 ${state['entry_peak']*(1-cfg['drop2']):.2f}\n{when}")
    if kind == "limit_set":
        return (f"📌 <b>TQQQ 1차 지정가 확정</b>\n"
                f"신호일(뉴욕 {state['limit1_day'][5:].replace('-', '/')}) 최저가 <b>${ev[2]:.2f}</b>\n\n"
                f"👉 SGOV 판 달러 전부로 TQQQ <b>${ev[2]:.2f} 지정가 매수</b> 조건주문\n"
                f"(프리·애프터 포함, 체결될 때까지 유지)\n"
                f"⚠️ 익절 신호 전까지 체결 안 되면 쫓아 사지 말고 취소\n{when}")
    if kind == "limit_fill":
        return (f"✅ <b>TQQQ 1차 지정가 도달</b>\n"
                f"가격이 ${ev[2]:.2f} 에 닿았습니다 (현재가 ${ev[3]:.2f}). 체결됐는지 확인하세요.\n{when}")
    if kind == "buy2":
        return (f"🟠 <b>TQQQ 2차 매수 신호</b>\n"
                f"최고점 ${state['entry_peak']:.2f} 대비 -{cfg['drop2']*100:.0f}% 도달 (현재가 ${ev[3]:.2f})\n\n"
                f"👉 할 일: <b>SGOV 전량 매도 → TQQQ 바로 매수</b> ({100-s}%)\n\n"
                f"기준 평균단가 ${state['avg']:.2f} → <b>새 익절 목표 ${state['avg']*(1+cfg['tp']):.2f}</b>\n"
                f"(익절 지정가 주문이 있다면 이 가격으로 수정)\n{when}")
    if kind == "sell":
        filled = ev[5] if len(ev) > 5 else True
        extra = "" if filled else "\n⚠️ 1차 지정가 미체결 → 그 주문은 취소하세요"
        return (f"🟢 <b>TQQQ 전량 익절 신호</b>\n"
                f"기준 평균단가 ${ev[4]:.2f} 대비 +{tp:.0f}% 도달 (현재가 ${ev[3]:.2f})\n\n"
                f"👉 할 일\n"
                f"① TQQQ <b>전량 매도</b> (목표가 ${ev[2]:.2f}){extra}\n"
                f"② 다음 주 월요일({next_monday_kst(t)}) <b>SGOV 전액 매수</b>\n\n"
                f"다음 1차 신호: 최고점 ${state['peak']:.2f} × {1-cfg['drop1']:.2f} = ${state['peak']*(1-cfg['drop1']):.2f}\n{when}")
    labels = {"near_buy1": "1차 신호가", "near_buy2": "2차 매수가", "near_tp": "익절가"}
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
        lines += ["상태: 💵 SGOV 파킹 대기",
                  f"1차 매수({s}%): ${b1:.2f} (현재가 대비 {(b1/p-1)*100:+.1f}%)",
                  f"2차 매수({100-s}%): ${b2:.2f} (현재가 대비 {(b2/p-1)*100:+.1f}%)"]
    else:
        tgt = state["avg"] * (1 + cfg["tp"])
        lines.append(f"상태: 📈 보유 중 ({'1차만' if state['phase']=='first' else '1차+2차'})")
        lines.append(f"기준가 1차 ${state['px1']:.2f}" + (f" / 2차 ${state['px2']:.2f}" if state.get("px2") else ""))
        if state.get("limit1"):
            st = ("체결" if state.get("limit1_filled") else "대기 중") if state.get("limit1_final") else "신호일 진행 중"
            lines.append(f"1차 지정가 ${state['limit1']:.2f} ({st})")
        lines.append(f"기준 평균단가 ${state['avg']:.2f} | 평가 {(p/state['avg']-1)*100:+.1f}%")
        if state["phase"] == "first":
            b2 = state["entry_peak"] * (1 - cfg["drop2"])
            lines.append(f"2차 매수({100-s}%): ${b2:.2f} (현재가 대비 {(b2/p-1)*100:+.1f}%)")
        lines.append(f"익절 목표: ${tgt:.2f} (현재가 대비 {(tgt/p-1)*100:+.1f}%)")
    lines.append(f"전략: -{cfg['drop1']*100:.0f}% {s}%(지정가) / -{cfg['drop2']*100:.0f}% {100-s}% / +{cfg['tp']*100:.0f}% 익절 · 대기자금 SGOV")
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

    events = []
    # 이전 버전 상태에서 넘어온 경우: 신호일 최저가를 과거 봉으로 다시 계산
    if state["phase"] != "flat" and not state.get("limit1_day") and state.get("buy1_time"):
        b1 = parse_ts(state["buy1_time"])
        day = et_date(b1)
        hist = fetch_bars(b1 - timedelta(seconds=1))
        same = [x for x in hist if et_date(x[0]) == day]
        after = [x for x in hist if et_date(x[0]) > day]
        if same:
            state.update(limit1_day=day, limit1=min(x[3] for x in same), limit1_final=False, limit1_filled=False)
            if after or datetime.now(ET).date().isoformat() > day:
                state["limit1_final"] = True
                events.append(("limit_set", now_utc(), state["limit1"], (after or same)[-1][4]))
                hit = [x for x in after if x[3] <= state["limit1"]]
                if hit:
                    state["limit1_filled"] = True
                    events.append(("limit_fill", hit[0][0], state["limit1"], after[-1][4]))

    events += process_bars(state, bars, cfg)

    # 신호일 장(애프터 20:00 ET)이 끝났으면 다음 날 봉을 기다리지 않고 지정가 확정
    if (state["phase"] != "flat" and state.get("limit1_day") and not state.get("limit1_final")):
        n = datetime.now(ET)
        if n.date().isoformat() > state["limit1_day"] or (n.date().isoformat() == state["limit1_day"] and n.hour >= 20):
            state["limit1_final"] = True
            events.append(("limit_set", now_utc(), state["limit1"], state.get("last_price") or state["limit1"]))
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
