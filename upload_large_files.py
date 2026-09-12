#!/usr/bin/env python3
r"""
Google Photos 대용량 동영상 전용 업로드 스크립트 v1.0
===================================================
50MB 이상 대용량 동영상(skipped)만 추출하여 
Single-Worker, 10분 Timeout 환경에서 1개씩 순차 전송.
"""

import os
import sys
import time
import sqlite3
import argparse
import mimetypes
import logging
from pathlib import Path
from typing import Optional

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

BASE_DIR            = Path(__file__).parent
TOKEN_FILE          = BASE_DIR / "token.json"
CLIENT_SECRETS_FILE = BASE_DIR / "client_secrets.json"
DB_FILE             = BASE_DIR / "upload_progress.db"

SCOPES = ["https://www.googleapis.com/auth/photoslibrary.appendonly"]
UPLOAD_URL       = "https://photoslibrary.googleapis.com/v1/uploads"
BATCH_CREATE_URL = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"
ALBUMS_URL       = "https://photoslibrary.googleapis.com/v1/albums"

UPLOAD_TIMEOUT   = 1800  # 초대형 파일 30분 타임아웃
DAILY_QUOTA      = 10_000
QUOTA_SAFETY_MARGIN = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "upload_large.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    CYAN   = "\033[96m"

def get_credentials() -> Credentials:
    if not TOKEN_FILE.exists():
        raise RuntimeError(f"Token file not found at {TOKEN_FILE}")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
    return creds

def get_today_quota_used(conn: sqlite3.Connection) -> int:
    today = time.strftime("%Y-%m-%d")
    row = conn.execute("SELECT used FROM quota_log WHERE date = ?", (today,)).fetchone()
    return row[0] if row else 0

def increment_quota(conn: sqlite3.Connection, amount: int = 1):
    today = time.strftime("%Y-%m-%d")
    conn.execute("INSERT OR IGNORE INTO quota_log (date, used) VALUES (?, 0)", (today,))
    conn.execute("UPDATE quota_log SET used = used + ?, updated_at = datetime('now') WHERE date = ?", (amount, today))
    conn.commit()

def upload_large_video(creds: Credentials, abs_path: str) -> Optional[str]:
    """10분 타임아웃으로 대용량 파일 바이너리 스트리밍 전송"""
    file_name = os.path.basename(abs_path)
    mime_type, _ = mimetypes.guess_type(abs_path)
    if not mime_type:
        mime_type = "video/mp4"

    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/octet-stream",
        "X-Goog-Upload-File-Name": urllib.parse.quote(file_name),
        "X-Goog-Upload-Protocol": "raw",
    }

    try:
        with open(abs_path, "rb") as fp:
            resp = requests.post(UPLOAD_URL, headers=headers, data=fp, timeout=UPLOAD_TIMEOUT)
        if resp.status_code == 200:
            return resp.text.strip()
        else:
            logger.warning(f"업로드 실패 ({resp.status_code}): {abs_path[-50:]} | {resp.text[:100]}")
            return None
    except Exception as e:
        logger.warning(f"업로드 예외: {abs_path[-50:]} | {e}")
        return None

def batch_create_item(creds: Credentials, album_id: Optional[str], token: str, abs_path: str) -> bool:
    file_name = os.path.basename(abs_path)
    item = {
        "description": file_name,
        "simpleMediaItem": {"uploadToken": token}
    }
    body = {"newMediaItems": [item]}
    if album_id:
        body["albumId"] = album_id

    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(BATCH_CREATE_URL, headers=headers, json=body, timeout=60)
        if resp.status_code == 200:
            res_json = resp.json()
            results = res_json.get("newMediaItemResults", [])
            if results and results[0].get("mediaItem", {}).get("id"):
                return True
        logger.warning(f"batchCreate 실패: {abs_path[-50:]} | {resp.text[:100]}")
        return False
    except Exception as e:
        logger.warning(f"batchCreate 예외: {abs_path[-50:]} | {e}")
        return False

import urllib.parse

def main():
    print(f"\n{C.BOLD}{C.CYAN}==================================================")
    print("      Google Photos 대용량 전용 업로더 v1.0")
    print(f"=================================================={C.RESET}\n")

    creds = get_credentials()
    conn = sqlite3.connect(str(DB_FILE))
    
    # skipped 상태인 50MB 이상 대용량 파일만 조회
    rows = conn.execute("""
        SELECT abs_path, file_size, album_name FROM files 
        WHERE status = 'skipped' AND file_size >= 50 * 1024 * 1024
        ORDER BY file_size ASC
    """).fetchall()

    total_targets = len(rows)
    print(f"{C.GREEN}[조회 완료]{C.RESET} 업로드 대상 대용량 동영상: {total_targets:,}개\n")

    if total_targets == 0:
        print("전송할 대용량 동영상이 없습니다!")
        return

    success_cnt = 0
    fail_cnt = 0

    for idx, (abs_path, file_size, album_name) in enumerate(rows, 1):
        used = get_today_quota_used(conn)
        if used >= DAILY_QUOTA - QUOTA_SAFETY_MARGIN:
            print(f"\n\n{C.YELLOW}[일시 정지]{C.RESET} 오늘 자 API 쿼터 소진 ({used}/10,000). 내일 재개해 주세요.")
            break

        size_mb = file_size / (1024 * 1024)
        fname = os.path.basename(abs_path)
        print(f"[{idx}/{total_targets}] ({size_mb:.1f} MB) {fname} 전송 중...", end="", flush=True)

        if 'samsung' in abs_path.lower() or '삼성' in abs_path:
            print(f" {C.YELLOW}[Samsung 폴더 제외]{C.RESET}")
            conn.execute("UPDATE files SET status = 'excluded' WHERE abs_path = ?", (abs_path,))
            conn.commit()
            continue

        if not os.path.exists(abs_path):
            print(f" {C.RED}[파일 없음]{C.RESET}")
            fail_cnt += 1
            continue

        # 1. OAuth 토큰 검증
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())

        # 2. 바이트 업로드
        upload_token = upload_large_video(creds, abs_path)
        increment_quota(conn, 1)

        if not upload_token:
            print(f" {C.RED}[업로드 실패]{C.RESET}")
            fail_cnt += 1
            continue

        # 3. batchCreate 직전 토큰 재검증 (대용량 파일은 업로드에 수 시간 걸려
        #    2단계 도중 토큰이 만료되는 경우가 있음 - 2019.12.23 하나음악회.mp4에서
        #    3회 연속 401로 확인됨)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        ok = batch_create_item(creds, None, upload_token, abs_path)
        if ok:
            conn.execute("""
                UPDATE files SET status = 'success', updated_at = datetime('now') WHERE abs_path = ?
            """, (abs_path,))
            conn.commit()
            print(f" {C.GREEN}[성공]{C.RESET}")
            success_cnt += 1
        else:
            print(f" {C.RED}[등록 실패]{C.RESET}")
            fail_cnt += 1

    print(f"\n{C.BOLD}==================================================")
    print(f" 대용량 전송 결과: 성공 {success_cnt:,}개 / 실패 {fail_cnt:,}개")
    print(f"=================================================={C.RESET}\n")
    conn.close()

if __name__ == "__main__":
    main()
