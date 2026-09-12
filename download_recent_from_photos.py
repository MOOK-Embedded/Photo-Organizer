#!/usr/bin/env python3
r"""
Google Photos 최근 사진 다운로더 (Download Recent Photos from Google Photos)
=======================================================================
목적:
  구글 포토에만 존재하는 최근 사진(2024~2026년)을 안전하게 D:\_GooglePhotos_Recent_Downloads에 다운로드.
  - photoslibrary.readonly 스코프 사용
  - baseUrl=d (이미지 원본) / baseUrl=dv (동영상 원본) 다운로드
  - D:\Photos_Merged 기존 보유 여부 대조하여 미보유 파일만 선별 다운로드
  - 생성일시(creationTime) 기준 mtime 복원
  - 다운로드 감사 저널: D:\_Reports\google_photos_recent_downloads_journal.csv
"""

import os
import sys
import time
import json
import csv
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

BASE_DIR = Path(r"D:\photos_uploader")
CLIENT_SECRETS_FILE = BASE_DIR / "client_secrets.json"
TOKEN_FILE = BASE_DIR / "token_readwrite.json"

SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.readonly",
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
]

DEFAULT_TARGET_DIR = Path(r"D:\_GooglePhotos_Recent_Downloads")
MERGED_DIR = Path(r"D:\Photos_Merged")
REPORT_FILE = Path(r"D:\_Reports\google_photos_recent_downloads_journal.csv")

MEDIA_ITEMS_URL = "https://photoslibrary.googleapis.com/v1/mediaItems"
SEARCH_URL = "https://photoslibrary.googleapis.com/v1/mediaItems:search"


def get_credentials() -> Credentials:
    """OAuth 2.0 인증 (읽기/쓰기 통합 스코프)"""
    creds = None
    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                print("[INFO] 만료된 토큰을 갱신합니다...")
                creds.refresh(Request())
            except Exception as e:
                print(f"[WARN] 토큰 갱신 실패: {e}. 브라우저 재인증을 진행합니다.")
                creds = None

        if not creds:
            if not CLIENT_SECRETS_FILE.exists():
                print(f"[ERROR] {CLIENT_SECRETS_FILE} 파일이 없습니다.")
                sys.exit(1)

            print("\n" + "=" * 70)
            print("[인증 안내] 브라우저 창이 자동으로 열립니다. Google 계정으로 로그인 후 허용을 클릭해 주세요.")
            print("=" * 70 + "\n")

            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS_FILE), SCOPES)
            creds = flow.run_local_server(port=0, prompt="consent")

        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
        print(f"[OK] 통합 토큰 저장 완료: {TOKEN_FILE}")

    return creds


def get_existing_signatures() -> set:
    """D:\Photos_Merged 내 기존 파일들의 (파일명, 파일크기) 시그니처 수집"""
    print("[1/4] D:\\Photos_Merged 기존 파일 목록 인덱싱 중...")
    signatures = set()
    if MERGED_DIR.exists():
        for root, _, files in os.walk(MERGED_DIR):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    sz = os.path.getsize(fp)
                    signatures.add((f.lower(), sz))
                except OSError:
                    pass
    print(f"  총 {len(signatures):,}개 기존 파일 인덱싱 완료")
    return signatures


def search_recent_photos(creds: Credentials, start_date: datetime) -> list:
    """start_date 이후의 미디어 아이템 목록 검색"""
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }

    body = {
        "pageSize": 100,
        "filters": {
            "dateFilter": {
                "ranges": [{
                    "startDate": {
                        "year": start_date.year,
                        "month": start_date.month,
                        "day": start_date.day,
                    },
                    "endDate": {
                        "year": 2030,
                        "month": 12,
                        "day": 31,
                    },
                }]
            }
        },
    }

    items = []
    page_token = None
    page_count = 0

    print(f"\n[2/4] 구글 포토 API로 {start_date.strftime('%Y-%m-%d')} 이후 사진 검색 중...")

    while True:
        if page_token:
            body["pageToken"] = page_token

        # 토큰 만료 대응
        if creds.expired:
            creds.refresh(Request())
            headers["Authorization"] = f"Bearer {creds.token}"

        resp = requests.post(SEARCH_URL, headers=headers, json=body, timeout=30)
        if resp.status_code == 401:
            creds.refresh(Request())
            headers["Authorization"] = f"Bearer {creds.token}"
            resp = requests.post(SEARCH_URL, headers=headers, json=body, timeout=30)

        if resp.status_code != 200:
            print(f"[ERROR] API 호출 실패 ({resp.status_code}): {resp.text[:200]}")
            break

        data = resp.json()
        new_items = data.get("mediaItems", [])
        items.extend(new_items)
        page_count += 1
        print(f"  페이지 {page_count}: 누적 {len(items):,}개 발견...")

        page_token = data.get("nextPageToken")
        if not page_token:
            break

        time.sleep(0.5)

    print(f"  검색 완료: 총 {len(items):,}개 미디어 아이템 발견")
    return items


def download_media_items(items: list, target_dir: Path, existing_signatures: set, dry_run: bool = False):
    """미디어 아이템 다운로드 실행"""
    target_dir.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)

    journal = []
    download_count = 0
    skip_count = 0

    print(f"\n[3/4] 다운로드 대상 분석 및 다운로드 실행 (대상 폴더: {target_dir})")

    for i, item in enumerate(items, 1):
        mid = item.get("id")
        filename = item.get("filename", f"photo_{mid[:10]}.jpg")
        base_url = item.get("baseUrl")
        meta = item.get("mediaMetadata", {})
        creation_time_str = meta.get("creationTime", "")
        is_video = "video" in meta

        # 연도 서브폴더 결정
        year_str = "Unknown"
        dt_obj = None
        if creation_time_str:
            try:
                dt_obj = datetime.fromisoformat(creation_time_str.replace("Z", "+00:00"))
                year_str = str(dt_obj.year)
            except Exception:
                pass

        year_dir = target_dir / year_str
        out_path = year_dir / filename

        # 다운로드 URL 구성 (=d: 원본 이미지, =dv: 원본 동영상)
        dl_url = f"{base_url}=dv" if is_video else f"{base_url}=d"

        # 이미 로컬 파일이 존재하는지 확인
        if out_path.exists():
            skip_count += 1
            journal.append({
                "media_id": mid,
                "filename": filename,
                "creation_time": creation_time_str,
                "status": "SKIPPED_ALREADY_ON_DISK",
                "save_path": str(out_path),
            })
            continue

        if dry_run:
            download_count += 1
            journal.append({
                "media_id": mid,
                "filename": filename,
                "creation_time": creation_time_str,
                "status": "DRY_RUN_PENDING",
                "save_path": str(out_path),
            })
            continue

        # 실제 다운로드 실행
        year_dir.mkdir(parents=True, exist_ok=True)
        try:
            resp = requests.get(dl_url, stream=True, timeout=60)
            if resp.status_code == 200:
                with open(out_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)

                # 파일 수정시간(mtime)을 촬영시각으로 복원
                if dt_obj:
                    ts = dt_obj.timestamp()
                    os.utime(out_path, (ts, ts))

                download_count += 1
                journal.append({
                    "media_id": mid,
                    "filename": filename,
                    "creation_time": creation_time_str,
                    "status": "DOWNLOADED_SUCCESS",
                    "save_path": str(out_path),
                })
                print(f"  [{i}/{len(items)}] OK: {year_str}/{filename}")
            else:
                print(f"  [{i}/{len(items)}] FAIL({resp.status_code}): {filename}")
                journal.append({
                    "media_id": mid,
                    "filename": filename,
                    "creation_time": creation_time_str,
                    "status": f"FAILED_{resp.status_code}",
                    "save_path": str(out_path),
                })
        except Exception as e:
            print(f"  [{i}/{len(items)}] ERROR: {filename} ({e})")
            journal.append({
                "media_id": mid,
                "filename": filename,
                "creation_time": creation_time_str,
                "status": f"ERROR_{e}",
                "save_path": str(out_path),
            })

    # 저널 저장
    if journal:
        keys = ["media_id", "filename", "creation_time", "status", "save_path"]
        with open(REPORT_FILE, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(journal)
        print(f"\n[4/4] 다운로드 저널 저장 완료: {REPORT_FILE}")

    print("\n" + "=" * 60)
    print(f"다운로드 완료 요약:")
    print(f"  - 신규 다운로드 성공: {download_count:,}건")
    print(f"  - 기존 보유로 스킵  : {skip_count:,}건")
    print(f"  - 총 검사 항목      : {len(items):,}건")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Google Photos 최근 사진 다운로더")
    parser.add_argument("--since", default="2024-01-01", help="검색 시작 날짜 (YYYY-MM-DD, 기본: 2024-01-01)")
    parser.add_argument("--target", default=str(DEFAULT_TARGET_DIR), help="다운로드 대상 폴더")
    parser.add_argument("--dry-run", action="store_true", help="실제 다운로드 없이 대상 목록만 조회")
    args = parser.parse_args()

    try:
        since_date = datetime.strptime(args.since, "%Y-%m-%d")
    except ValueError:
        print(f"[ERROR] 잘못된 날짜 형식: {args.since} (YYYY-MM-DD 형식으로 입력하세요)")
        sys.exit(1)

    print(f"=== 구글 포토 최근 사진 다운로드 파이프라인 ===")
    print(f"  시작 기준일: {since_date.strftime('%Y-%m-%d')}")
    print(f"  저장 대상  : {args.target}")
    print(f"  모드       : {'DRY RUN (목록만 조회)' if args.dry_run else '실제 다운로드'}")
    print("=" * 50)

    creds = get_credentials()
    existing_sigs = get_existing_signatures()
    items = search_recent_photos(creds, since_date)

    if not items:
        print("\n[INFO] 해당 기간에 등록된 새로운 사진이 없습니다.")
        return

    download_media_items(items, Path(args.target), existing_sigs, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
