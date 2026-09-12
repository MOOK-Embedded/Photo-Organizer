"""
token_ready 상태 파일들을 즉시 batchCreate로 Google Photos에 등록합니다.
uploadToken이 아직 유효한 파일들을 즉시 처리합니다.
"""
import sqlite3
import json
import logging
import sys
import time
from pathlib import Path

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# ── 설정
TOKEN_FILE = Path("token.json")
DB_FILE = Path("upload_progress.db")
SCOPES = ["https://www.googleapis.com/auth/photoslibrary.appendonly"]
BATCH_CREATE_URL = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"
ALBUMS_URL = "https://photoslibrary.googleapis.com/v1/albums"
BATCH_SIZE = 50

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(DB_FILE.parent / "upload.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ── 인증
creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
if creds.expired and creds.refresh_token:
    creds.refresh(Request())
    logger.info("토큰 갱신 완료")

def auth_header():
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}

# ── DB 연결
conn = sqlite3.connect(str(DB_FILE), timeout=30)
conn.row_factory = sqlite3.Row

def create_fresh_album(name: str) -> str | None:
    """앨범을 새로 생성하고 DB를 갱신합니다."""
    for attempt in range(3):
        try:
            resp = requests.post(
                ALBUMS_URL,
                headers=auth_header(),
                json={"album": {"title": name}},
                timeout=30,
            )
            if resp.status_code == 200:
                aid = resp.json().get("id")
                if aid:
                    # DB 갱신 (기존 레코드 삭제 후 새 ID로 교체)
                    conn.execute("DELETE FROM albums WHERE name = ?", (name,))
                    conn.execute("INSERT INTO albums (name, album_id) VALUES (?, ?)", (name, aid))
                    conn.commit()
                    logger.info(f"앨범 생성 완료: {name} -> {aid[:30]}...")
                    return aid
            logger.warning(f"앨범 생성 실패 ({resp.status_code}): {name} | {resp.text[:100]}")
            time.sleep(2 ** attempt)
        except Exception as e:
            logger.warning(f"앨범 생성 예외: {e}")
            time.sleep(2 ** attempt)
    return None

def get_or_create_album(name: str, album_cache: dict) -> str | None:
    """캐시에서 앨범 ID를 반환. 없으면 새로 생성."""
    if name in album_cache:
        return album_cache[name]
    aid = create_fresh_album(name)
    if aid:
        album_cache[name] = aid
    return aid

# ── token_ready 파일 가져오기
cur = conn.cursor()

# 앨범 캐시는 빈 딕셔너리로 시작 (DB 캐시 무시 - 모두 무효일 수 있음)
# 404 발생 시 자동으로 재생성됩니다
album_cache = {}

cur.execute("""
    SELECT id, abs_path, album_name, upload_token FROM files
    WHERE status = 'token_ready' AND upload_token IS NOT NULL
    ORDER BY album_name, id
""")
token_ready_files = [dict(r) for r in cur.fetchall()]

print(f"\n=== token_ready 파일 {len(token_ready_files):,}개 batchCreate 시작 ===\n")

if not token_ready_files:
    print("처리할 token_ready 파일이 없습니다.")
    conn.close()
    exit()

# 앨범별 그룹화
from collections import defaultdict
album_groups = defaultdict(list)
for f in token_ready_files:
    album_groups[f["album_name"]].append(f)

success_total = 0
fail_total = 0
skip_total = 0

for album_name, files in album_groups.items():
    album_id = get_or_create_album(album_name, album_cache)
    if not album_id:
        logger.warning(f"앨범 생성 불가, 건너뜀: {album_name}")
        skip_total += len(files)
        continue

    # 50개씩 배치 처리
    for i in range(0, len(files), BATCH_SIZE):
        batch = files[i:i + BATCH_SIZE]
        new_media_items = [
            {
                "simpleMediaItem": {
                    "uploadToken": f["upload_token"],
                    "fileName": Path(f["abs_path"]).name,
                }
            }
            for f in batch
        ]
        body = {"albumId": album_id, "newMediaItems": new_media_items}

        for attempt in range(3):
            try:
                resp = requests.post(BATCH_CREATE_URL, headers=auth_header(), json=body, timeout=60)
                if resp.status_code == 200:
                    data = resp.json()
                    results = data.get("newMediaItemResults", [])
                    for idx, result in enumerate(results):
                        item = batch[idx]
                        media_item = result.get("mediaItem", {})
                        status_info = result.get("status", {})
                        if media_item.get("id"):
                            # ✅ 성공
                            conn.execute("""
                                UPDATE files
                                SET media_item_id = ?, status = 'success', updated_at = datetime('now')
                                WHERE id = ?
                            """, (media_item["id"], item["id"]))
                            success_total += 1
                        else:
                            err_code = status_info.get("code", 0)
                            err_msg = status_info.get("message", "")
                            logger.warning(
                                f"mediaItem 실패 (code={err_code}): {err_msg} | {item['abs_path'][-50:]}"
                            )
                            # uploadToken 만료 → pending으로 리셋
                            conn.execute("""
                                UPDATE files
                                SET upload_token = NULL, status = 'pending', updated_at = datetime('now')
                                WHERE id = ?
                            """, (item["id"],))
                            fail_total += 1
                    conn.commit()
                    break  # 성공 시 재시도 루프 탈출

                elif resp.status_code == 429:
                    print(f"\n[쿼터 초과] 오늘 업로드 중단. 내일 재실행하세요.")
                    conn.commit()
                    conn.close()
                    exit()
                elif resp.status_code == 404:
                    # 앨범 ID 무효 → 캐시 삭제 후 새로 생성하여 재시도
                    logger.warning(f"앨범 ID 404 → 재생성: {album_name}")
                    album_cache.pop(album_name, None)
                    new_aid = create_fresh_album(album_name)
                    if new_aid:
                        album_id = new_aid
                        body["albumId"] = album_id
                        # attempt 루프 계속 (재시도)
                    else:
                        logger.warning(f"앨범 재생성 실패, 건너뜀: {album_name}")
                        skip_total += len(batch)
                        break
                    time.sleep(1)
                elif resp.status_code == 429:
                    print(f"\n[쿼터 초과] 오늘 업로드 중단. 내일 재실행하세요.")
                    conn.commit()
                    conn.close()
                    exit()
                else:
                    logger.warning(f"batchCreate 실패 ({resp.status_code}): {resp.text[:200]}")
                    time.sleep(2 ** attempt)

            except Exception as e:
                logger.warning(f"batchCreate 예외 (시도 {attempt+1}/3): {e}")
                time.sleep(2 ** attempt)

        # 진행 상황 출력
        processed = success_total + fail_total + skip_total
        pct = processed / len(token_ready_files) * 100
        print(f"\r  [{processed:>5}/{len(token_ready_files):>5}] {pct:5.1f}%  OK={success_total}  FAIL={fail_total}", end="", flush=True)

conn.commit()
conn.close()

print(f"\n\n=== 완료 ===")
print(f"  성공 (Google Photos 등록): {success_total:,}개")
print(f"  실패 (uploadToken 만료 등): {fail_total:,}개 → pending 리셋됨")
print(f"  앨범 생성 실패 건너뜀: {skip_total:,}개")
print(f"\n※ 실패한 파일들은 다음에 python upload_to_photos.py 실행 시 재업로드됩니다.")
