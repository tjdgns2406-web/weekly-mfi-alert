import os
import sys
import requests

def main():
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()

    print(f"--- 환경변수 점검 ---")
    print(f"TOKEN 길이: {len(token)}")
    print(f"CHAT_ID 값: '{chat_id}'")
    print(f"CHAT_ID가 숫자냐?: {chat_id.isdigit() or (chat_id.startswith('-') and chat_id[1:].isdigit())}")
    print(f"----------------------")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": "테스트 메시지입니다!"})
    
    print(f"응답 코드: {resp.status_code}")
    print(f"응답 내용: {resp.text}")

if __name__ == "__main__":
    main()
