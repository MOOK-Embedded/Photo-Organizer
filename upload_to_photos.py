#!/usr/bin/env python3
r"""
Google Photos 대량 업로드 스크립트 v2.0
========================================
개선사항 (v2.0):
  1. Producer-Consumer 큐: 업로드 완료 즉시 batchCreate 실행 (uploadToken 만료 방지)
  2. 강건한 토큰 갱신: 네트워크 불안정 시에도 반복 재시도 + graceful 종료
  3. 네트워크 자동 감지: 연속 실패 시 대기 후 재개, 쿼터 낭비 방지
  4. 세밀한 에러 분류: SSL/Timeout/HTTP 오류별 개별 처리 및 재시도
  5. 앨범 메모리 캐시: 세션 내 유효한 앨범 ID만 재사용 (404 무효 ID 방지)
"""

import os
import sys
import time
import queue
import sqlite3
import hashlib
import argparse
import mimetypes
import logging
import threading
import urllib.parse
import concurrent.futures
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from requests.exceptions import SSLError, ConnectionError, Timeout, ReadTimeout

# ─────────────────────────────────────────────
# 설정 상수
# ─────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/photoslibrary.appendonly"]

SUPPORTED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif",
    ".gif", ".webp", ".bmp", ".tiff", ".tif",
    ".mp4", ".mov", ".avi", ".mkv", ".3gp",
    ".m4v", ".wmv",
}

DAILY_QUOTA         = 10_000   # Google Photos API 일일 쿼터
BATCH_SIZE          = 50       # batchCreate 최대 항목 수
MAX_WORKERS         = 2        # 업로드 병렬 스레드 수 (안정성 우선)
QUOTA_SAFETY_MARGIN = 200      # 쿼터 최소 잔여치

# 네트워크 재시도 설정 (Fast Fail로 변경)
UPLOAD_TIMEOUT      = 120      # 파일 업로드 타임아웃 (초)
API_TIMEOUT         = 60       # API 요청 타임아웃 (초)
MAX_RETRY           = 3        # 최대 재시도 횟수
BASE_BACKOFF        = 2        # 첫 재시도 대기 (초)
MAX_BACKOFF         = 30       # 최대 대기 (초)
NET_FAIL_THRESHOLD  = 8        # 이 횟수 연속 실패 시 네트워크 점검 대기
NET_WAIT_SECONDS    = 15       # 네트워크 불안정 판단 시 대기 (초)

BASE_DIR            = Path(__file__).parent
TOKEN_FILE          = BASE_DIR / "token.json"
CLIENT_SECRETS_FILE = BASE_DIR / "client_secrets.json"
DB_FILE             = BASE_DIR / "upload_progress.db"

UPLOAD_URL      = "https://photoslibrary.googleapis.com/v1/uploads"
BATCH_CREATE_URL= "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"
ALBUMS_URL      = "https://photoslibrary.googleapis.com/v1/albums"

# ─────────────────────────────────────────────
# 로깅
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "upload.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# ANSI 색상
# ─────────────────────────────────────────────
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    CYAN   = "\033[96m"
    BLUE   = "\033[94m"
    GRAY   = "\033[90m"


# ─────────────────────────────────────────────
# 예외 클래스
# ─────────────────────────────────────────────
class QuotaExceededException(Exception):
    pass

class NetworkUnavailableException(Exception):
    pass


# ─────────────────────────────────────────────
# SQLite 데이터베이스
# ─────────────────────────────────────────────
class UploadDB:
    """업로드 진행 상황 SQLite 기록/조회 (스레드 안전)"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._local  = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS files (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                rel_path       TEXT NOT NULL,
                abs_path       TEXT NOT NULL UNIQUE,
                file_size      INTEGER NOT NULL,
                mtime          REAL NOT NULL,
                album_name     TEXT,
                upload_token   TEXT,
                media_item_id  TEXT,
                status         TEXT NOT NULL DEFAULT 'pending',
                fail_count     INTEGER NOT NULL DEFAULT 0,
                created_at     TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS albums (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL UNIQUE,
                album_id   TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS quota_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                date       TEXT NOT NULL,
                used       INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(date)
            );

            CREATE INDEX IF NOT EXISTS idx_files_status   ON files(status);
            CREATE INDEX IF NOT EXISTS idx_files_abs_path ON files(abs_path);
            CREATE INDEX IF NOT EXISTS idx_files_album    ON files(album_name, status);
        """)
        # 기존 DB에 fail_count 컬럼이 없을 경우 추가
        try:
            conn.execute("ALTER TABLE files ADD COLUMN fail_count INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        # 기존 DB에 content_hash 컬럼이 없을 경우 추가 (콘텐츠 기반 중복 업로드 방지)
        try:
            conn.execute("ALTER TABLE files ADD COLUMN content_hash TEXT")
        except Exception:
            pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_hash ON files(content_hash)")
        conn.commit()
        conn.close()

    # ── 파일 등록
    def bulk_upsert_files(self, targets: list[dict]):
        """
        content_hash가 이미 success로 존재하면(로컬 경로만 이동/변경되었거나
        AGY가 이미 업로드한 동일 파일) 재업로드 없이 success로 즉시 매핑한다.
        """
        conn = self._get_conn()
        conn.execute("BEGIN TRANSACTION;")
        try:
            for t in targets:
                content_hash = t.get("content_hash") or None
                dup = None
                if content_hash:
                    dup = conn.execute("""
                        SELECT media_item_id FROM files
                        WHERE content_hash = ? AND status = 'success' AND abs_path != ?
                        LIMIT 1
                    """, (content_hash, t["abs_path"])).fetchone()

                if dup:
                    conn.execute("""
                        INSERT INTO files (rel_path, abs_path, file_size, mtime, album_name,
                                            content_hash, media_item_id, status)
                        VALUES (:rel_path, :abs_path, :file_size, :mtime, :album_name,
                                :content_hash, :dup_media_id, 'success')
                        ON CONFLICT(abs_path) DO UPDATE SET
                            status        = 'success',
                            media_item_id = excluded.media_item_id,
                            content_hash  = excluded.content_hash,
                            updated_at    = datetime('now')
                        WHERE files.status NOT IN ('success', 'skipped')
                    """, {**t, "content_hash": content_hash, "dup_media_id": dup["media_item_id"]})
                else:
                    conn.execute("""
                        INSERT INTO files (rel_path, abs_path, file_size, mtime, album_name,
                                            content_hash, status)
                        VALUES (:rel_path, :abs_path, :file_size, :mtime, :album_name,
                                :content_hash, 'pending')
                        ON CONFLICT(abs_path) DO UPDATE SET
                            file_size    = excluded.file_size,
                            mtime        = excluded.mtime,
                            album_name   = excluded.album_name,
                            content_hash = excluded.content_hash,
                            updated_at   = datetime('now')
                        WHERE files.status NOT IN ('success', 'skipped')
                    """, {**t, "content_hash": content_hash})
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ── 상태 업데이트
    def set_upload_token(self, abs_path: str, token: str):
        conn = self._get_conn()
        conn.execute("""
            UPDATE files SET upload_token = ?, status = 'token_ready', updated_at = datetime('now')
            WHERE abs_path = ?
        """, (token, abs_path))
        conn.commit()

    def set_success(self, abs_path: str, media_item_id: str):
        conn = self._get_conn()
        conn.execute("""
            UPDATE files
            SET media_item_id = ?, status = 'success', upload_token = NULL,
                updated_at = datetime('now')
            WHERE abs_path = ?
        """, (media_item_id, abs_path))
        conn.commit()

    def mark_failed_with_count(self, abs_path: str):
        """실패 카운트 증가. 3회 이상이면 skipped 확정"""
        conn = self._get_conn()
        conn.execute("""
            UPDATE files SET
                upload_token = NULL,
                fail_count   = COALESCE(fail_count, 0) + 1,
                status       = CASE
                    WHEN COALESCE(fail_count, 0) + 1 >= 3 THEN 'skipped'
                    ELSE 'pending'
                END,
                updated_at = datetime('now')
            WHERE abs_path = ?
        """, (abs_path,))
        conn.commit()

    def reset_token_ready_to_pending(self) -> int:
        """프로그램 시작 시: 이전 uploadToken은 24시간 만료 → 재업로드"""
        conn = self._get_conn()
        conn.execute("""
            UPDATE files
            SET upload_token = NULL, status = 'pending', updated_at = datetime('now')
            WHERE status = 'token_ready'
        """)
        count = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        return count

    def reset_failed_to_pending(self) -> int:
        """fail_count < 3인 failed 파일 재시도"""
        conn = self._get_conn()
        conn.execute("""
            UPDATE files
            SET upload_token = NULL, status = 'pending', updated_at = datetime('now')
            WHERE status = 'failed' AND fail_count < 3
        """)
        count = conn.execute("SELECT changes()").fetchone()[0]
        # 3회 이상은 영구 skip
        conn.execute("""
            UPDATE files SET status = 'skipped', updated_at = datetime('now')
            WHERE status = 'failed' AND fail_count >= 3
        """)
        conn.commit()
        return count

    # ── 조회
    def get_pending_files(self) -> list:
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT * FROM files WHERE status = 'pending'
            ORDER BY album_name, rel_path
        """).fetchall()
        return [dict(r) for r in rows]

    def get_album_id(self, name: str) -> Optional[str]:
        conn = self._get_conn()
        row = conn.execute("SELECT album_id FROM albums WHERE name = ?", (name,)).fetchone()
        return row["album_id"] if row else None

    def save_album(self, name: str, album_id: str):
        conn = self._get_conn()
        conn.execute("DELETE FROM albums WHERE name = ?", (name,))
        conn.execute("INSERT INTO albums (name, album_id) VALUES (?, ?)", (name, album_id))
        conn.commit()

    def delete_album(self, name: str):
        conn = self._get_conn()
        conn.execute("DELETE FROM albums WHERE name = ?", (name,))
        conn.commit()

    def get_today_quota_used(self) -> int:
        conn = self._get_conn()
        today = datetime.now().strftime("%Y-%m-%d")
        row = conn.execute("SELECT used FROM quota_log WHERE date = ?", (today,)).fetchone()
        return row["used"] if row else 0

    def increment_quota(self, amount: int = 1):
        conn = self._get_conn()
        today = datetime.now().strftime("%Y-%m-%d")
        # INSERT OR IGNORE로 행 보장 후 UPDATE (SQLite 버전 호환)
        conn.execute("""
            INSERT OR IGNORE INTO quota_log (date, used) VALUES (?, 0)
        """, (today,))
        conn.execute("""
            UPDATE quota_log SET used = used + ?, updated_at = datetime('now')
            WHERE date = ?
        """, (amount, today))
        conn.commit()

    def get_stats(self) -> dict:
        conn = self._get_conn()
        row = conn.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status='success'    THEN 1 ELSE 0 END) AS success,
                SUM(CASE WHEN status='failed'     THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status='pending'    THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status='token_ready'THEN 1 ELSE 0 END) AS token_ready,
                SUM(CASE WHEN status='skipped'    THEN 1 ELSE 0 END) AS skipped
            FROM files
        """).fetchone()
        return dict(row)

    def close(self):
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


# ─────────────────────────────────────────────
# Google 인증
# ─────────────────────────────────────────────
def get_credentials() -> Credentials:
    """OAuth 2.0 인증. 토큰 없으면 브라우저 흐름으로 획득."""
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("액세스 토큰 갱신 중...")
            creds.refresh(Request())
        else:
            if not CLIENT_SECRETS_FILE.exists():
                print(f"\n{C.RED}[오류]{C.RESET} {CLIENT_SECRETS_FILE} 파일이 없습니다.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS_FILE), SCOPES)
            creds = flow.run_local_server(port=0, prompt="consent")

        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
        logger.info(f"토큰 저장 완료: {TOKEN_FILE}")

    return creds


def safe_refresh_token(creds: Credentials, max_attempts: int = 5) -> bool:
    """
    네트워크 불안정 상황에서도 반복 재시도하여 토큰 갱신.
    성공 시 True, 완전 실패 시 False 반환.
    """
    for attempt in range(max_attempts):
        try:
            creds.refresh(Request())
            with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
            logger.info("토큰 갱신 성공")
            return True
        except Exception as e:
            wait = min(BASE_BACKOFF * (2 ** attempt), MAX_BACKOFF)
            logger.warning(f"토큰 갱신 실패 (시도 {attempt+1}/{max_attempts}): {e} → {wait}초 후 재시도")
            time.sleep(wait)
    logger.error("토큰 갱신 완전 실패. 프로세스를 안전하게 종료합니다.")
    return False


# ─────────────────────────────────────────────
# 네트워크 상태 감시자
# ─────────────────────────────────────────────
class NetworkMonitor:
    """연속 실패 횟수를 추적하여 네트워크 불안정 여부를 판단"""

    def __init__(self, threshold: int = NET_FAIL_THRESHOLD):
        self._lock = threading.Lock()
        self._consecutive_fails = 0
        self._threshold = threshold

    def record_success(self):
        with self._lock:
            self._consecutive_fails = 0

    def record_fail(self):
        with self._lock:
            self._consecutive_fails += 1
            return self._consecutive_fails

    def is_unstable(self) -> bool:
        with self._lock:
            return self._consecutive_fails >= self._threshold

    def wait_if_unstable(self):
        """네트워크 불안정 감지 시 잠시 대기 후 카운터 리셋"""
        if self.is_unstable():
            logger.warning(
                f"[네트워크] 연속 {self._consecutive_fails}회 실패 감지. "
                f"{NET_WAIT_SECONDS}초 대기 후 재개..."
            )
            time.sleep(NET_WAIT_SECONDS)
            with self._lock:
                self._consecutive_fails = 0


# ─────────────────────────────────────────────
# API 헬퍼
# ─────────────────────────────────────────────
class PhotosAPI:
    def __init__(self, creds: Credentials, db: UploadDB, net_monitor: NetworkMonitor):
        self.creds        = creds
        self.db           = db
        self.net_monitor  = net_monitor
        self._lock        = threading.Lock()
        self._quota_event = threading.Event()
        self._album_cache: dict[str, str] = {}  # 세션 내 유효한 앨범 ID 캐시

    @property
    def quota_exceeded(self) -> bool:
        return self._quota_event.is_set()

    def _auth_header(self) -> dict:
        """토큰 만료 시 안전하게 갱신. 갱신 실패 시 예외 발생."""
        with self._lock:
            if self.creds.expired and self.creds.refresh_token:
                ok = safe_refresh_token(self.creds)
                if not ok:
                    raise NetworkUnavailableException("토큰 갱신 완전 실패")
        return {"Authorization": f"Bearer {self.creds.token}"}

    def _check_quota(self):
        used = self.db.get_today_quota_used()
        remaining = DAILY_QUOTA - used
        if remaining <= QUOTA_SAFETY_MARGIN:
            self._quota_event.set()
            raise QuotaExceededException(f"일일 쿼터 거의 소진 (사용={used}/{DAILY_QUOTA})")

    def _backoff(self, attempt: int) -> float:
        """지수 백오프 대기 시간 계산"""
        return min(BASE_BACKOFF * (2 ** attempt), MAX_BACKOFF)

    def upload_bytes(self, file_path: str, mime_type: str, file_size: int = 0) -> Optional[str]:
        """
        1단계: 파일 바이트 업로드 → uploadToken 반환.
        SSL/타임아웃/네트워크 에러에 강건한 재시도 (단, 대용량 파일은 빠른 포기).
        """
        if self.quota_exceeded:
            return None
        self._check_quota()

        with open(file_path, "rb") as f:
            data = f.read()

        safe_name = urllib.parse.quote(Path(file_path).name)
        headers = {
            **self._auth_header(),
            "Content-type": "application/octet-stream",
            "X-Goog-Upload-Content-Type": mime_type,
            "X-Goog-Upload-Protocol": "raw",
            "X-Goog-Upload-File-Name": safe_name,
        }

        for attempt in range(MAX_RETRY):
            self.net_monitor.wait_if_unstable()
            try:
                resp = requests.post(UPLOAD_URL, headers=headers, data=data,
                                     timeout=UPLOAD_TIMEOUT)
                self.db.increment_quota(1)

                if resp.status_code == 200:
                    self.net_monitor.record_success()
                    return resp.text.strip()

                if resp.status_code == 401:
                    # 토큰 만료 → 즉시 갱신 후 헤더 업데이트하여 재시도
                    logger.warning("업로드 401: 토큰 갱신 시도")
                    ok = safe_refresh_token(self.creds)
                    if not ok:
                        raise NetworkUnavailableException("401 후 토큰 갱신 실패")
                    headers["Authorization"] = f"Bearer {self.creds.token}"
                    continue  # 대기 없이 즉시 재시도

                if resp.status_code == 429:
                    self._quota_event.set()
                    raise QuotaExceededException("429 Rate Limit")

                if resp.status_code >= 500:
                    wait = self._backoff(attempt)
                    logger.warning(f"업로드 서버 오류({resp.status_code}), {wait:.0f}초 후 재시도")
                    self.net_monitor.record_fail()
                    time.sleep(wait)
                    continue

                logger.warning(f"업로드 실패 ({resp.status_code}): {file_path[-60:]}")
                return None  # 4xx (non-401) → 파일 자체 문제, 재시도 의미 없음

            except QuotaExceededException:
                raise
            except NetworkUnavailableException:
                raise
            except (SSLError, ConnectionError, Timeout, ReadTimeout) as e:
                cnt = self.net_monitor.record_fail()
                if file_size > 50 * 1024 * 1024:
                    logger.warning(f"대용량 파일({file_size/1024/1024:.1f}MB) 네트워크 오류 빠른 포기: {type(e).__name__} | {file_path[-60:]}")
                    return None
                wait = self._backoff(attempt)
                logger.warning(
                    f"업로드 네트워크 오류 (시도 {attempt+1}/{MAX_RETRY}, 연속실패:{cnt}): "
                    f"{type(e).__name__} → {wait:.0f}초 후 재시도"
                )
                time.sleep(wait)
            except Exception as e:
                cnt = self.net_monitor.record_fail()
                wait = self._backoff(attempt)
                logger.warning(f"업로드 예외 (시도 {attempt+1}/{MAX_RETRY}): {e} → {wait:.0f}초 후 재시도")
                time.sleep(wait)

        logger.error(f"업로드 최종 실패: {file_path[-80:]}")
        return None

    def get_or_create_album(self, album_name: str) -> Optional[str]:
        """앨범 ID 반환. 세션 캐시 우선, 없으면 새로 생성."""
        with self._lock:
            if album_name in self._album_cache:
                return self._album_cache[album_name]

        if self.quota_exceeded:
            return None
        self._check_quota()

        for attempt in range(MAX_RETRY):
            self.net_monitor.wait_if_unstable()
            try:
                resp = requests.post(
                    ALBUMS_URL,
                    headers={**self._auth_header(), "Content-Type": "application/json"},
                    json={"album": {"title": album_name}},
                    timeout=API_TIMEOUT,
                )
                self.db.increment_quota(1)

                if resp.status_code == 200:
                    album_id = resp.json().get("id")
                    if album_id:
                        self.db.save_album(album_name, album_id)
                        with self._lock:
                            self._album_cache[album_name] = album_id
                        self.net_monitor.record_success()
                        return album_id

                if resp.status_code == 401:
                    ok = safe_refresh_token(self.creds)
                    if not ok:
                        raise NetworkUnavailableException("401 후 토큰 갱신 실패")
                    continue

                if resp.status_code == 429:
                    self._quota_event.set()
                    raise QuotaExceededException("429 앨범 생성 쿼터 초과")

                wait = self._backoff(attempt)
                logger.warning(f"앨범 생성 실패({resp.status_code}): {album_name} → {wait:.0f}초 후 재시도")
                time.sleep(wait)

            except (QuotaExceededException, NetworkUnavailableException):
                raise
            except (SSLError, ConnectionError, Timeout) as e:
                cnt = self.net_monitor.record_fail()
                wait = self._backoff(attempt)
                logger.warning(f"앨범 생성 네트워크 오류(연속:{cnt}): {e} → {wait:.0f}초 후 재시도")
                time.sleep(wait)
            except Exception as e:
                wait = self._backoff(attempt)
                logger.warning(f"앨범 생성 예외: {e} → {wait:.0f}초 후 재시도")
                time.sleep(wait)

        return None

    def batch_create(self, album_id: str, items: list) -> dict:
        """
        2단계: batchCreate로 미디어 아이템 최종 등록 (최대 50개).
        404(앨범 ID 무효) 시 앨범 재생성 후 재시도.
        """
        if self.quota_exceeded:
            return {}
        self._check_quota()

        body = {
            "albumId": album_id,
            "newMediaItems": [
                {
                    "simpleMediaItem": {
                        "uploadToken": item["upload_token"],
                        "fileName": Path(item["abs_path"]).name,
                    }
                }
                for item in items
            ],
        }

        for attempt in range(MAX_RETRY):
            self.net_monitor.wait_if_unstable()
            try:
                resp = requests.post(
                    BATCH_CREATE_URL,
                    headers={**self._auth_header(), "Content-Type": "application/json"},
                    json=body,
                    timeout=API_TIMEOUT,
                )
                self.db.increment_quota(1)

                if resp.status_code == 200:
                    self.net_monitor.record_success()
                    return resp.json()

                if resp.status_code == 401:
                    ok = safe_refresh_token(self.creds)
                    if not ok:
                        raise NetworkUnavailableException("401 후 토큰 갱신 실패")
                    body["albumId"] = album_id  # 재시도
                    continue

                if resp.status_code == 404:
                    # 앨범 ID 무효 → 캐시에서 제거 후 재생성
                    logger.warning(f"batchCreate 404: 앨범 ID 무효 → 재생성 시도")
                    album_name = items[0].get("album_name", "") if items else ""
                    if album_name:
                        with self._lock:
                            self._album_cache.pop(album_name, None)
                        self.db.delete_album(album_name)
                        new_id = self.get_or_create_album(album_name)
                        if new_id:
                            album_id = new_id
                            body["albumId"] = album_id
                            continue
                    return {"error_code": 404}

                if resp.status_code == 429:
                    self._quota_event.set()
                    raise QuotaExceededException("429 batchCreate 쿼터 초과")

                wait = self._backoff(attempt)
                logger.warning(f"batchCreate 실패({resp.status_code}): {resp.text[:150]} → {wait:.0f}초 후 재시도")
                time.sleep(wait)

            except (QuotaExceededException, NetworkUnavailableException):
                raise
            except (SSLError, ConnectionError, Timeout) as e:
                cnt = self.net_monitor.record_fail()
                wait = self._backoff(attempt)
                logger.warning(f"batchCreate 네트워크 오류(연속:{cnt}): {e} → {wait:.0f}초 후 재시도")
                time.sleep(wait)
            except Exception as e:
                wait = self._backoff(attempt)
                logger.warning(f"batchCreate 예외: {e} → {wait:.0f}초 후 재시도")
                time.sleep(wait)

        return {}


# ─────────────────────────────────────────────
# 파일 스캔
# ─────────────────────────────────────────────
_HASH_CHUNK = 131072  # 128KB


def compute_partial_hash(file_path: str, file_size: int) -> Optional[str]:
    """
    앞/뒤 128KB + 파일 크기를 조합한 부분 해시.
    동일 콘텐츠가 로컬에서 이동/이름변경되었거나 AGY가 이미 업로드한 경우를
    감지하여 abs_path만으로는 걸러지지 않는 중복 업로드를 방지한다.
    """
    h = hashlib.sha1()
    h.update(str(file_size).encode())
    try:
        with open(file_path, "rb") as f:
            h.update(f.read(_HASH_CHUNK))
            if file_size > _HASH_CHUNK:
                f.seek(max(0, file_size - _HASH_CHUNK))
                h.update(f.read(_HASH_CHUNK))
    except (OSError, PermissionError):
        return None
    return h.hexdigest()


def scan_files(source_dir: Path, excludes: list[str]) -> tuple[list[dict], list[dict]]:
    upload_targets = []
    skipped_files  = []
    exclude_set    = {Path(e).resolve() for e in excludes}

    print(f"\n{C.CYAN}[스캔 중]{C.RESET} {source_dir} 하위 파일 탐색...\n")
    scan_start = datetime.now()

    for root, dirs, files in os.walk(source_dir, followlinks=False):
        root_path = Path(root)
        dirs[:] = [
            d for d in dirs
            if (root_path / d).resolve() not in exclude_set
            and not d.startswith("$")
            and d not in {"System Volume Information", "Recovery", "WindowsApps"}
        ]

        for file_name in files:
            file_path = root_path / file_name
            ext = file_path.suffix.lower()
            try:
                stat      = file_path.stat()
                file_size = stat.st_size
                mtime     = stat.st_mtime
            except (PermissionError, OSError):
                continue

            if ext in SUPPORTED_EXTENSIONS:
                try:
                    rel_path = str(file_path.relative_to(source_dir))
                except ValueError:
                    rel_path = str(file_path)

                try:
                    album_name = str(file_path.parent.relative_to(source_dir))
                except ValueError:
                    album_name = str(file_path.parent)

                if album_name == ".":
                    album_name = source_dir.name or "ROOT"

                # 50MB 초과 파일은 업로드 대상에서 영구 제외되므로 해시 계산 생략
                content_hash = (
                    compute_partial_hash(str(file_path), file_size)
                    if file_size <= 50 * 1024 * 1024 else None
                )

                upload_targets.append({
                    "rel_path":     rel_path,
                    "abs_path":     str(file_path),
                    "file_size":    file_size,
                    "mtime":        mtime,
                    "album_name":   album_name,
                    "content_hash": content_hash,
                    "ext":          ext,
                })
            else:
                skipped_files.append({
                    "abs_path":  str(file_path),
                    "ext":       ext,
                    "file_size": file_size,
                })

    elapsed = (datetime.now() - scan_start).total_seconds()
    print(f"\n{C.GREEN}[스캔 완료]{C.RESET} {elapsed:.1f}초 소요, {len(upload_targets):,}개 대상")
    return upload_targets, skipped_files


def write_skipped_report(skipped_files: list[dict], upload_targets: list[dict]):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = BASE_DIR / f"skipped_files_{ts}.txt"
    ext_counter: dict[str, int] = {}
    for f in skipped_files:
        ext_counter[f["ext"]] = ext_counter.get(f["ext"], 0) + 1

    with open(report_path, "w", encoding="utf-8") as fp:
        fp.write(f"# 건너뛴 파일 목록 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})\n")
        for f in skipped_files:
            fp.write(f"{f['abs_path']} | {f['ext']} | {f['file_size']}\n")
        total = len(upload_targets) + len(skipped_files)
        fp.write(f"\n총 스캔: {total:,}  업로드대상: {len(upload_targets):,}  건너뜀: {len(skipped_files):,}\n")
        for ext, cnt in sorted(ext_counter.items(), key=lambda x: -x[1]):
            fp.write(f"  {ext or '(없음)':<15} {cnt:>6}개\n")

    print(f"\n{C.YELLOW}[스킵 리포트]{C.RESET} {report_path}")
    total = len(upload_targets) + len(skipped_files)
    print(f"  총 스캔: {total:,}  업로드대상: {len(upload_targets):,}  건너뜀: {len(skipped_files):,}")
    for ext, cnt in sorted(ext_counter.items(), key=lambda x: -x[1])[:10]:
        print(f"    {ext or '(없음)':<15} {cnt:>6}개")


# ─────────────────────────────────────────────
# 진행 상황 표시
# ─────────────────────────────────────────────
class ProgressTracker:
    def __init__(self, total: int, db: UploadDB):
        self.total      = total
        self.db         = db
        self._lock      = threading.Lock()
        self.done       = 0
        self.success    = 0
        self.skipped    = 0
        self.failed     = 0
        self.start_time = time.time()

    def update(self, success=0, failed=0, skipped=0):
        with self._lock:
            self.done    += success + failed + skipped
            self.success += success
            self.failed  += failed
            self.skipped += skipped
            self._print()

    def _print(self):
        elapsed    = time.time() - self.start_time
        pct        = (self.done / self.total * 100) if self.total > 0 else 0
        quota_used = self.db.get_today_quota_used()
        quota_rem  = DAILY_QUOTA - quota_used
        remaining  = self.total - self.done

        if self.done > 0 and elapsed > 0:
            rate      = self.done / elapsed
            req_per_f = 1.02
            daily_f   = int(quota_rem / req_per_f)
            days_left = max(0, (remaining - daily_f) / (DAILY_QUOTA / req_per_f)) if remaining > daily_f else 0
            eta_str   = f"{days_left:.1f}일" if days_left > 0.1 else "오늘 완료 가능"
        else:
            eta_str = "계산 중..."

        bar_len = 30
        filled  = int(bar_len * pct / 100)
        bar     = "#" * filled + "-" * (bar_len - filled)

        line = (
            f"\r{C.CYAN}[{bar}]{C.RESET} {pct:5.1f}%  "
            f"{C.GREEN}[OK] {self.success}{C.RESET} "
            f"{C.RED}[FAIL] {self.failed}{C.RESET} "
            f"{C.GRAY}[SKIP] {self.skipped}{C.RESET}  "
            f"Quota:{C.YELLOW}{quota_rem:,}{C.RESET}  "
            f"ETA:{C.BLUE}{eta_str}{C.RESET}  "
            f"({self.done}/{self.total})"
        )
        print(line, end="", flush=True)


# ─────────────────────────────────────────────
# 업로드 엔진 (Producer-Consumer)
# ─────────────────────────────────────────────
# 센티넬: batchCreate 워커 종료 신호
_SENTINEL = object()


def upload_worker(api: PhotosAPI, db: UploadDB, file_info: dict,
                  token_queue: queue.Queue, tracker: ProgressTracker):
    """
    [Producer] 단일 파일을 업로드하여 uploadToken을 획득.
    성공 시 token_queue에 (album_name, file_info_with_token) 을 넣는다.
    """
    abs_path  = file_info["abs_path"]
    file_size = file_info.get("file_size", 0)

    # 50MB 이상 동영상은 일단 스킵 (병목 및 무한 대기 방지)
    if file_size > 50 * 1024 * 1024:
        logger.warning(f"병목 방지를 위해 50MB 이상 파일 영구 스킵: {abs_path[-60:]}")
        # fail_count를 3으로 만들어 즉시 skipped 처리
        for _ in range(3):
            db.mark_failed_with_count(abs_path)
        tracker.update(skipped=1)
        return

    mime_type, _ = mimetypes.guess_type(abs_path)
    ext = Path(abs_path).suffix.lower()
    if not mime_type:
        mime_type = "image/jpeg" if ext in {".jpg", ".jpeg", ".heic", ".heif"} else "video/mp4"

    try:
        token = api.upload_bytes(abs_path, mime_type, file_info.get("file_size", 0))
        if token:
            db.set_upload_token(abs_path, token)
            token_queue.put((file_info["album_name"], {**file_info, "upload_token": token}))
        else:
            db.mark_failed_with_count(abs_path)
            tracker.update(failed=1)
    except QuotaExceededException:
        raise
    except NetworkUnavailableException:
        raise
    except Exception as e:
        logger.warning(f"업로드 워커 예외: {abs_path[-60:]}: {e}")
        db.mark_failed_with_count(abs_path)
        tracker.update(failed=1)


def batch_create_worker(api: PhotosAPI, db: UploadDB,
                        token_queue: queue.Queue, tracker: ProgressTracker):
    """
    [Consumer] token_queue에서 토큰이 쌓이면 즉시 batchCreate로 앨범에 등록.
    앨범별로 50개씩 묶어서 처리.
    """
    album_buffers: dict[str, list] = defaultdict(list)

    def flush_album(album_name: str):
        """album_name 버퍼의 파일들을 batchCreate로 즉시 등록"""
        buf = album_buffers.get(album_name, [])
        if not buf:
            return

        album_id = api.get_or_create_album(album_name)
        if not album_id:
            logger.warning(f"앨범 생성 실패, {len(buf)}개 pending 리셋: {album_name}")
            for item in buf:
                db.mark_failed_with_count(item["abs_path"])
                tracker.update(failed=1)
            album_buffers[album_name] = []
            return

        # 50개씩 배치 처리
        for i in range(0, len(buf), BATCH_SIZE):
            if api.quota_exceeded:
                break
            batch = buf[i:i + BATCH_SIZE]
            result = api.batch_create(album_id, batch)

            if not result or "error_code" in result:
                # batchCreate 실패 → 파일들 pending으로 리셋 (다음 실행 시 재시도)
                for item in batch:
                    db.mark_failed_with_count(item["abs_path"])
                    tracker.update(failed=1)
                logger.warning(f"batchCreate 실패 {len(batch)}건 → fail_count 증가")
                continue

            for idx, entry in enumerate(result.get("newMediaItemResults", [])):
                item       = batch[idx]
                media_item = entry.get("mediaItem", {})
                status_val = entry.get("status", {})
                if media_item.get("id"):
                    db.set_success(item["abs_path"], media_item["id"])
                    tracker.update(success=1)
                else:
                    err_code = status_val.get("code", 0)
                    err_msg  = status_val.get("message", "")
                    logger.warning(f"mediaItem 실패(code={err_code}): {err_msg} | {item['abs_path'][-50:]}")
                    db.mark_failed_with_count(item["abs_path"])
                    tracker.update(failed=1)

        album_buffers[album_name] = []

    while True:
        try:
            item = token_queue.get(timeout=5)
        except queue.Empty:
            # 버퍼에 남은 항목 flush
            for album_name in list(album_buffers.keys()):
                if album_buffers[album_name]:
                    flush_album(album_name)
            continue

        if item is _SENTINEL:
            # 종료 신호: 남은 버퍼 전부 flush 후 종료
            for album_name in list(album_buffers.keys()):
                if album_buffers[album_name]:
                    flush_album(album_name)
            break

        if api.quota_exceeded:
            # 쿼터 초과 시 남은 항목들 DB에 token_ready로 유지 (uploadToken은 DB에 이미 저장됨)
            token_queue.task_done()
            continue

        album_name, file_with_token = item
        album_buffers[album_name].append(file_with_token)

        # 버퍼가 BATCH_SIZE에 도달하면 즉시 flush
        if len(album_buffers[album_name]) >= BATCH_SIZE:
            flush_album(album_name)

        token_queue.task_done()


# ─────────────────────────────────────────────
# 메인 업로드 로직
# ─────────────────────────────────────────────
def run_upload(
    source_dir: Path,
    excludes: list[str],
    test_mode: bool = False,
    test_folder: Optional[str] = None,
    dry_run: bool = False,
):
    # ── 인증
    print(f"\n{C.BOLD}[1/6] Google 인증{C.RESET}")
    creds = get_credentials()
    print(f"  {C.GREEN}[OK] 인증 완료{C.RESET}")

    # ── DB 초기화
    db = UploadDB(DB_FILE)

    # ── 파일 스캔
    print(f"\n{C.BOLD}[2/6] 파일 스캔{C.RESET}")
    scan_source = Path(test_folder) if test_folder else source_dir
    if test_folder:
        print(f"  테스트 모드: {scan_source}")

    upload_targets, skipped_files = scan_files(scan_source, excludes)
    write_skipped_report(skipped_files, upload_targets)

    if not upload_targets:
        print(f"\n{C.YELLOW}업로드할 파일이 없습니다.{C.RESET}")
        return

    if test_mode and not test_folder:
        upload_targets = upload_targets[:20]
        print(f"\n{C.YELLOW}[테스트 모드] 처음 20개 파일만 처리합니다.{C.RESET}")

    # ── 쿼터 예측
    total_files  = len(upload_targets)
    total_size_gb = sum(f["file_size"] for f in upload_targets) / (1024**3)
    est_requests = total_files + (total_files // BATCH_SIZE) + 1
    est_days     = est_requests / DAILY_QUOTA

    print(f"\n{C.BOLD}[3/6] 업로드 예측{C.RESET}")
    print(f"  총 파일 수      : {total_files:,}개")
    print(f"  총 용량         : {total_size_gb:.2f} GB")
    print(f"  예상 API 요청   : {est_requests:,}회")
    print(f"  예상 소요 일수  : {est_days:.1f}일 (일일 {DAILY_QUOTA:,} 쿼터 기준)")

    today_quota_used = db.get_today_quota_used()
    today_remaining  = DAILY_QUOTA - today_quota_used
    print(f"  오늘 남은 쿼터  : {today_remaining:,}회")

    if dry_run:
        print(f"\n{C.YELLOW}[DRY RUN] 실제 업로드 없이 종료합니다.{C.RESET}")
        db.close()
        return

    # ── 진행 확인
    print(f"\n{C.BOLD}[4/6] 진행 확인{C.RESET}")
    print("  자동 진행 설정으로 바로 업로드를 진행합니다.")

    # ── DB 등록
    print(f"\n{C.BOLD}[5/6] DB 등록{C.RESET}")
    db.bulk_upsert_files(upload_targets)

    # token_ready 리셋 (이전 uploadToken은 24시간 만료)
    reset_count = db.reset_token_ready_to_pending()
    if reset_count > 0:
        print(f"  {C.YELLOW}[INFO] token_ready {reset_count:,}개 → pending 리셋 (토큰 만료 대응){C.RESET}")
        logger.info(f"token_ready {reset_count}개 pending 리셋")

    fail_reset = db.reset_failed_to_pending()
    if fail_reset > 0:
        print(f"  {C.YELLOW}[INFO] failed {fail_reset:,}개 → pending 리셋 (재시도){C.RESET}")

    pending = db.get_pending_files()
    print(f"  처리 대기 파일  : {len(pending):,}개 (이미 완료 제외)")

    if not pending:
        print(f"\n{C.GREEN}[OK] 모든 파일이 이미 업로드 완료되어 있습니다!{C.RESET}")
        db.close()
        return

    # ── 업로드 시작
    print(f"\n{C.BOLD}[6/6] 업로드 시작{C.RESET}  (Ctrl+C로 중단 가능)\n")
    net_monitor = NetworkMonitor()
    api         = PhotosAPI(creds, db, net_monitor)
    tracker     = ProgressTracker(len(pending), db)

    # Producer-Consumer: 업로드 완료 즉시 batchCreate 큐
    token_queue = queue.Queue(maxsize=200)

    # Consumer 스레드 시작
    consumer_thread = threading.Thread(
        target=batch_create_worker,
        args=(api, db, token_queue, tracker),
        daemon=True,
        name="batchCreate-worker"
    )
    consumer_thread.start()

    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=MAX_WORKERS,
            thread_name_prefix="uploader"
        ) as executor:
            futures = {}
            for f in pending:
                if api.quota_exceeded:
                    break
                fut = executor.submit(upload_worker, api, db, f, token_queue, tracker)
                futures[fut] = f

            for future in concurrent.futures.as_completed(futures):
                if api.quota_exceeded:
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                try:
                    future.result()
                except (QuotaExceededException, NetworkUnavailableException):
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                except Exception as e:
                    logger.warning(f"업로드 퓨처 예외: {e}")

    except QuotaExceededException:
        print(f"\n\n{C.RED}{'='*60}")
        print(f"  일일 쿼터 소진! 오늘 업로드 중단.")
        print(f"{'='*60}{C.RESET}")
        print(f"\n  {C.YELLOW}내일 같은 명령으로 재실행하면 이어서 진행됩니다.{C.RESET}")
        print(f"  python upload_to_photos.py\n")

    except NetworkUnavailableException as e:
        print(f"\n\n{C.RED}{'='*60}")
        print(f"  네트워크/인증 오류로 업로드를 안전하게 중단합니다.")
        print(f"  사유: {e}")
        print(f"{'='*60}{C.RESET}")
        print(f"\n  {C.YELLOW}네트워크 확인 후 재실행하면 이어서 진행됩니다.{C.RESET}\n")

    except KeyboardInterrupt:
        print(f"\n\n{C.YELLOW}[중단] 사용자가 중단했습니다. 재실행 시 이어서 진행됩니다.{C.RESET}\n")

    finally:
        # Consumer에 종료 신호 보내고 남은 배치 처리 대기
        token_queue.put(_SENTINEL)
        consumer_thread.join(timeout=120)

        stats      = db.get_stats()
        quota_used = db.get_today_quota_used()
        print(f"\n\n{C.BOLD}{'='*60}")
        print(f"  최종 통계")
        print(f"{'='*60}{C.RESET}")
        print(f"  성공        : {C.GREEN}{stats['success']:,}{C.RESET}")
        print(f"  실패        : {C.RED}{stats['failed']:,}{C.RESET}")
        print(f"  대기중      : {stats['pending']:,}")
        print(f"  영구스킵    : {stats['skipped']:,}")
        print(f"  오늘 쿼터   : {quota_used:,}/{DAILY_QUOTA:,} 사용\n")
        db.close()


# ─────────────────────────────────────────────
# CLI 진입점
# ─────────────────────────────────────────────
def main():
    global MAX_WORKERS
    parser = argparse.ArgumentParser(
        description="Google Photos 대량 업로드 v2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python upload_to_photos.py                         # 전체 D:\\ 업로드
  python upload_to_photos.py --workers 3             # 병렬 3스레드
  python upload_to_photos.py --test                  # 처음 20개만 테스트
  python upload_to_photos.py --test-folder "D:\\사진" # 특정 폴더만
  python upload_to_photos.py --dry-run               # 스캔만 (업로드 없음)
        """,
    )
    parser.add_argument("--source",       default="D:\\",   help="소스 루트 경로 (기본: D:\\)")
    parser.add_argument("--exclude",      action="append",  default=[], metavar="FOLDER", help="제외 폴더")
    parser.add_argument("--test",         action="store_true",  help="처음 20개만 테스트")
    parser.add_argument("--test-folder",  metavar="PATH",        help="지정 폴더만 테스트")
    parser.add_argument("--dry-run",      action="store_true",  help="스캔만, 업로드 없음")
    parser.add_argument("--workers",      type=int, default=2,  help="병렬 업로드 스레드 수 (기본: 2)")

    args = parser.parse_args()
    MAX_WORKERS = args.workers

    if sys.platform == "win32":
        os.system("color")

    print(f"""{C.BOLD}{C.CYAN}
==================================================
      Google Photos Bulk Uploader v2.0
      Source: {args.source}
==================================================
{C.RESET}""")

    run_upload(
        source_dir=Path(args.source),
        excludes=args.exclude,
        test_mode=args.test,
        test_folder=args.test_folder,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
