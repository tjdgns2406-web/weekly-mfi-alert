"""
MFI 3중 조건 스크리너 -> 텔레그램 알림 (하이킨아시 기준)
- 일봉 MFI(14) <= 30
- 주봉 MFI(14, 특정 종목 11) <= 30  (마감된 직전 주봉)
- 월봉 MFI(14, 특정 종목 11) <= 50  (마감된 직전 월봉)
- 월봉 데이터가 부족한 신규 상장 종목은 월봉 조건을 생략(N/A 표시)
- 세 조건 모두 충족(AND) 시 이름순 알림, 전 거래일 대비 신규/이탈 표시
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
    "BOX", "BRK.B", "BWXT", "CARR", "CCJ", "CEG", "CGNX", "CIFR", "CLS",
    "CLSK", "COHR", "COIN", "COST", "CPER", "CRCL", "CRDO", "CRM", "CRSP", "CRWV",
    "DAL", "DDOG", "DE", "DELL", "DFH", "DFEN", "DGRO", "DIS", "DIVB", "DIVO",
    "DVA", "EEM", "ELF", "EMR", "ENTG", "ETU", "EWL", "F", "FAS",
    "FCX", "FIS", "FLR", "FRO", "GD", "GEV", "GLW", "GME", "GOOG", "GOOGL",
    "HALO", "HD", "HIMS", "HOOD", "HUT", "IBM", "IEMG", "IGV", "ILMN", "INOD",
    "INTC", "INTU", "IONQ", "IREN", "IWB", "JCI", "JNJ", "JOBY", "JPM", "KO",
    "KTOS", "LHX", "LLY", "LMT", "LNG", "LRCX", "LULU", "LUNR", "MCD", "MDB",
    "META", "MMM", "MP", "MRNA", "MRVL", "MSFT", "MSI", "MSTR", "MU", "NAIL",
    "NBIS", "NEE", "NFLX", "NKE", "NOK", "NOW", "NRIX", "NTRA", "NU", "NVDA",
    "O", "OKLO", "ORCL", "OXY", "PANW", "PATH", "PEP", "PFE", "PG", "PHM",
    "PL", "PLTR", "PM", "PPA", "QCOM", "QLD", "QQQ", "QBTS", "QUBT", "RCAT",
    "RDDT", "RDW", "RDVY", "RGTI", "RKLB", "ROBO", "SCHD", "SMCI", "SMMT", "SMR",
    "SNDK", "SNOW", "SNPS", "SO", "SOFI", "SONY", "SOUN", "SOXL", "SOXX",
    "SPOT", "STRL", "STX", "SYM", "T", "TCOM", "TE",
    "TEM", "TER", "TFC", "TGTX", "TM", "TME", "TOL", "TQQQ", "TRIN", "TSLA",
    "TSEM", "TT", "TXN", "U", "UBER", "UCO", "UGL", "ULTA", "UNH", "UPST",
    "UTHR", "VEA", "VIG", "VKTX", "VLO", "VOOG", "VRT", "VST", "VYM", "WDC",
    "WM", "WMT", "WULF", "XLC", "XLE", "XLF", "XLK", "XLY", "XOM",
    "438080.KS", "381170.KS", "494670.KS", "244580.KS", "0082V0.KS",
    "396500.KS", "411060.KS", "473460.KS", "438100.KS"
]

# 주봉/월봉에서 MFI(11)을 쓰는 종목
MFI_11_TICKERS = {
    "VIG", "VYM", "RDVY", "DGRO", "DIVB", "SCHD", "DIVO",
    "SNDK", "MU", "GEV", "AMD", "VLO", "ABNB"
}

# 메시지에 표시할 한글 이름 (없는 종목은 티커만 표시됨)
NAMES = {
    # --- 미국 ---
    "AAOI": "어플라이드옵토일렉트로닉스", "AAPL": "애플", "ABNB": "에어비앤비",
    "ACM": "AECOM", "ADBE": "어도비", "AGQ": "프로셰어즈 실버2배",
    "ALAB": "아스테라랩스", "AMAT": "어플라이드머티리얼즈", "AMD": "AMD",
    "AMPH": "앰퍼스트", "AMZN": "아마존", "ANET": "아리스타네트웍스",
    "APH": "암페놀", "ARKF": "ARK 핀테크혁신", "ASTS": "AST스페이스모바일",
    "AVAV": "에어로바이런먼트", "AVGO": "브로드컴", "AXON": "액손",
    "AXP": "아메리칸익스프레스", "AXTI": "AXT",
    "BA": "보잉", "BABA": "알리바바", "BAC": "뱅크오브아메리카",
    "BBAI": "빅베어AI", "BE": "블룸에너지", "BEAM": "빔테라퓨틱스",
    "BIDU": "바이두", "BITX": "비트코인2배(볼트에이지)", "BMY": "브리스톨마이어스",
    "BOTZ": "글로벌X 로봇&AI", "BOX": "박스", "BRK.B": "버크셔해서웨이B",
    "BWXT": "BWX테크놀로지스",
    "CARR": "캐리어글로벌", "CCJ": "카메코", "CEG": "컨스텔레이션에너지",
    "CGNX": "코그넥스", "CIFR": "사이퍼마이닝", "CLS": "셀레스티카",
    "CLSK": "클린스파크", "COHR": "코히런트", "COIN": "코인베이스",
    "COST": "코스트코", "CPER": "미국구리펀드", "CRCL": "서클",
    "CRDO": "크레도테크놀로지", "CRM": "세일즈포스", "CRSP": "크리스퍼테라퓨틱스",
    "CRWV": "코어위브",
    "DAL": "델타항공", "DDOG": "데이터독", "DE": "디어앤컴퍼니",
    "DELL": "델테크놀로지스", "DFH": "드림파인더스홈즈", "DFEN": "디렉시온 방산3배",
    "DGRO": "iShares 배당성장", "DIS": "디즈니", "DIVB": "iShares 우량배당",
    "DIVO": "아미플렉스 배당옵션인컴", "DVA": "다비타",
    "EEM": "iShares 신흥국", "ELF": "엘프뷰티", "EMR": "에머슨일렉트릭",
    "ENTG": "엔테그리스", "EWL": "iShares 스위스",
    "F": "포드", "FAS": "디렉시온 금융3배", "FCX": "프리포트맥모란",
    "FIS": "피델리티내셔널인포메이션", "FLR": "플루어", "FRO": "프런트라인",
    "GD": "제너럴다이내믹스", "GEV": "GE베르노바", "GLW": "코닝",
    "GME": "게임스탑", "GOOG": "알파벳C", "GOOGL": "알파벳A",
    "HALO": "할로자임", "HD": "홈디포", "HIMS": "힘스앤허스",
    "HOOD": "로빈후드", "HUT": "헛8",
    "IBM": "IBM", "IEMG": "iShares 코어신흥국", "IGV": "iShares 소프트웨어",
    "ILMN": "일루미나", "INOD": "이노데이타", "INTC": "인텔",
    "INTU": "인튜이트", "IONQ": "아이온큐", "IREN": "아이렌",
    "IWB": "iShares 러셀1000",
    "JCI": "존슨컨트롤즈", "JNJ": "존슨앤존슨", "JOBY": "조비에비에이션",
    "JPM": "JP모건",
    "KO": "코카콜라", "KTOS": "크라토스",
    "LHX": "L3해리스", "LLY": "일라이릴리", "LMT": "록히드마틴",
    "LNG": "셰니에르에너지", "LRCX": "램리서치", "LULU": "룰루레몬",
    "LUNR": "인튜이티브머신스",
    "MCD": "맥도날드", "MDB": "몽고DB", "META": "메타", "MMM": "3M",
    "MP": "MP머티리얼즈", "MRNA": "모더나", "MRVL": "마벨",
    "MSFT": "마이크로소프트", "MSI": "모토로라솔루션즈", "MSTR": "스트래티지",
    "MU": "마이크론",
    "NAIL": "디렉시온 주택건설3배", "NBIS": "네비우스", "NEE": "넥스트에라에너지",
    "NFLX": "넷플릭스", "NKE": "나이키", "NOK": "노키아", "NOW": "서비스나우",
    "NRIX": "뉴릭스", "NTRA": "나테라", "NU": "누홀딩스", "NVDA": "엔비디아",
    "O": "리얼티인컴", "OKLO": "오클로", "ORCL": "오라클", "OXY": "옥시덴탈",
    "PANW": "팔로알토", "PATH": "유아이패스", "PEP": "펩시코", "PFE": "화이자",
    "PG": "P&G", "PHM": "풀티그룹", "PL": "플래닛랩스", "PLTR": "팔란티어",
    "PM": "필립모리스", "PPA": "인베스코 항공방산",
    "QCOM": "퀄컴", "QLD": "프로셰어즈 나스닥2배", "QQQ": "인베스코 QQQ",
    "QBTS": "디웨이브퀀텀", "QUBT": "퀀텀컴퓨팅",
    "RCAT": "레드캣", "RDDT": "레딧", "RDW": "레드와이어",
    "RDVY": "퍼스트트러스트 라이징배당", "RGTI": "리게티컴퓨팅", "RKLB": "로켓랩",
    "ROBO": "ROBO 글로벌로보틱스",
    "SCHD": "슈왑 미국배당", "SMCI": "슈퍼마이크로", "SMMT": "서밋테라퓨틱스",
    "SMR": "뉴스케일파워", "SNDK": "샌디스크", "SNOW": "스노우플레이크",
    "SNPS": "시높시스", "SO": "서던컴퍼니", "SOFI": "소파이", "SONY": "소니",
    "SOUN": "사운드하운드AI", "SOXL": "디렉시온 반도체3배", "SOXX": "iShares 반도체",
    "SPOT": "스포티파이", "STRL": "스털링인프라", "STX": "시게이트",
    "SYM": "심보틱",
    "T": "AT&T", "TCOM": "트립닷컴", "TE": "T1에너지", "TEM": "템퍼스AI",
    "TER": "테라다인", "TFC": "트루이스트", "TGTX": "TG테라퓨틱스",
    "TM": "도요타", "TME": "텐센트뮤직", "TOL": "톨브라더스",
    "TQQQ": "프로셰어즈 나스닥3배", "TRIN": "트리니티캐피탈", "TSLA": "테슬라",
    "TSEM": "타워세미컨덕터", "TT": "트레인테크놀로지스", "TXN": "텍사스인스트루먼트",
    "U": "유니티", "UBER": "우버", "UCO": "프로셰어즈 원유2배",
    "UGL": "프로셰어즈 금2배", "ULTA": "울타뷰티", "UNH": "유나이티드헬스",
    "UPST": "업스타트", "UTHR": "유나이티드테라퓨틱스",
    "VEA": "뱅가드 선진국", "VIG": "뱅가드 배당성장", "VKTX": "바이킹테라퓨틱스",
    "VLO": "발레로에너지", "VOOG": "뱅가드 S&P500성장", "VRT": "버티브",
    "VST": "비스트라", "VYM": "뱅가드 고배당",
    "WDC": "웨스턴디지털", "WM": "웨이스트매니지먼트", "WMT": "월마트",
    "WULF": "테라울프",
    "XLC": "커뮤니케이션 SPDR", "XLE": "에너지 SPDR", "XLF": "금융 SPDR",
    "XLK": "기술 SPDR", "XLY": "경기소비재 SPDR", "XOM": "엑슨모빌",
    # --- 국내 ETF ---
    "438080.KS": "ACE 미국S&P500",
    "381170.KS": "TIGER 미국테크TOP",
    "494670.KS": "TIGER 조선TOP10",
    "244580.KS": "KODEX 바이오",
    "0082V0.KS": "KODEX TDF2060액티브",
    "396500.KS": "TIGER 반도체TOP10",
    "411060.KS": "ACE KRX금현물",
    "473460.KS": "KODEX 미국서학개미",
    "438100.KS": "ACE 미국나스닥100",
}


def label(t: str) -> str:
    """한글이름(티커). 이름이 없으면 티커만 표시"""
    name = NAMES.get(t)
    code = t.replace(".KS", "")
    return f"{name}({code})" if name else code


USE_HEIKIN_ASHI = True   # False로 바꾸면 일반 캔들로 계산
DEFAULT_MFI_PERIOD = 14
ALT_MFI_PERIOD = 11      # 주봉/월봉 예외 종목만
DAILY_THRESHOLD = 30
WEEKLY_THRESHOLD = 30
MONTHLY_THRESHOLD = 50
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
    if len(df) < period + 1:
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
    if d is None or w is None:
        return None
    m_ok = True if m is None else m <= MONTHLY_THRESHOLD  # 월봉 데이터 부족 시 조건 생략
    ok = d <= DAILY_THRESHOLD and w <= WEEKLY_THRESHOLD and m_ok
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
                hits.append(now)
                if not prev_ok:
                    new_entries.append(t)
            elif prev_ok:
                exited.append(t)

            m_txt = "N/A" if now["m"] is None else f"{now['m']:.1f}"
            print(f"[OK]   {t:<10} 일봉 {now['d']:5.1f} | 주봉 {now['w']:5.1f} | 월봉 {m_txt}")
        except Exception as e:
            failed.append(t)
            print(f"[FAIL] {t:<10} 건너뜀: {e}", file=sys.stderr)

    # 표시되는 이름 기준으로 정렬 (가나다/ABC순)
    hits.sort(key=lambda x: label(x["ticker"]))
    new_entries.sort(key=label)
    exited.sort(key=label)
    return hits, new_entries, exited, failed


def build_message(hits, new_entries, exited) -> str:
    title = (f"일봉<={DAILY_THRESHOLD} / 주봉<={WEEKLY_THRESHOLD} / "
             f"월봉<={MONTHLY_THRESHOLD}")
    if not hits and not exited:
        return f"[📉MFI 3중 조건] ({title})\n충족 종목이 없습니다."

    lines = [f"[📉MFI 3중 조건] ({title}) - {len(hits)}개", ""]
    for h in hits:
        period = f" (주·월 MFI{h['p']})" if h["p"] == ALT_MFI_PERIOD else ""
        m_txt = "N/A(데이터 부족)" if h["m"] is None else f"{h['m']:.1f}"
        lines.append(
            f"- {label(h['ticker'])}: 일봉 {h['d']:.1f} | 주봉 {h['w']:.1f} | 월봉 {m_txt}{period}"
        )

    lines.append("\n----------------------------------")
    if new_entries:
        lines.append(f"🆕 새로 추가된 종목 ({len(new_entries)}개):")
        lines.append("- " + ", ".join(label(x) for x in new_entries))
    else:
        lines.append("🆕 새로 추가된 종목: 없음")
    lines.append("")
    if exited:
        lines.append(f"💹 목록에서 이탈한 종목 ({len(exited)}개):")
        lines.append("- " + ", ".join(label(x) for x in exited))
    else:
        lines.append("💹 목록에서 이탈한 종목: 없음")
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

    message = build_message(hits, new_entries, exited)
    if failed:
        message += f"\n\n⚠️ 데이터 조회 실패: {', '.join(label(x) for x in failed)}"
    try:
        send_telegram(token, chat_id, message)
        print("\n텔레그램 전송 완료:\n" + message)
    except Exception as e:
        print(f"\n텔레그램 전송 실패: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
