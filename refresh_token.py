"""
토큰 강제 갱신 스크립트
"""
import json
from pathlib import Path
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

TOKEN_FILE = Path("token.json")
SCOPES = ["https://www.googleapis.com/auth/photoslibrary.appendonly"]

try:
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    print(f"현재 토큰 만료 여부: {creds.expired}")
    print(f"refresh_token 있음: {bool(creds.refresh_token)}")

    # 강제 갱신
    creds.refresh(Request())
    print(f"[OK] 토큰 갱신 성공")
    print(f"새 만료 시각: {creds.expiry}")

    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    print(f"[OK] token.json 저장 완료")

except Exception as e:
    print(f"[ERROR] 토큰 갱신 실패: {e}")
    print("네트워크 확인 후 다시 시도하세요.")
