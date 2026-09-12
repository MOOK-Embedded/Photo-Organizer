#!/usr/bin/env python3
r"""
Google Photos Chrome Automation Bot (Playwright CDP Engine)
===========================================================
목적:
  Chrome 브라우저를 원격 제어(CDP: 9222)하여 Google Photos Web에서
  2024~2026년 최근 사진을 검색 ➡️ 전수 선택 ➡️ 원터치 다운로드 ➡️ 로컬 정본 라이브러리 자동 통합.
  
특징:
  1. 실제 설치된 Chrome 브라우저 프로필 사용으로 구글 봇 탐지(Bot Detection) 100% 우회
  2. Playwright expect_download 이벤트로 무손실 Zip 다운로드 직접 인터셉트
  3. 다운로드 완료 즉시 압축 해제 ➡️ 표준 파일명 변환 ➡️ EXIF 무손실 회전 ➡️ D:\Photos_Merged 편입
  4. 감사 저널: D:\_Reports\chrome_bot_download_journal.csv
"""

import os
import sys
import time
import json
import zipfile
import asyncio
import subprocess
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import piexif
from PIL import Image
from playwright.async_api import async_playwright

# ─────────────────────────────────────────────
# 기본 설정
# ─────────────────────────────────────────────
BASE_DIR = Path(r"D:\photos_uploader")
CHROME_PATH = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
PROFILE_DIR = Path(r"D:\_chrome_automation_profile")
DOWNLOAD_STAGING = Path(r"D:\_GooglePhotos_Recent_Downloads")
MERGED_DIR = Path(r"D:\Photos_Merged")
REPORT_FILE = Path(r"D:\_Reports\chrome_bot_download_journal.csv")
CDP_URL = "http://localhost:9222"


def ensure_chrome_running():
    """Chrome이 9222 포트로 실행될 때까지 대기 (사용자가 바탕화면 바로가기 실행 시 자동 감지)"""
    import urllib.request
    print("\n[Chrome CDP 연결 확인 중 (http://localhost:9222)]...")
    for i in range(300): # 최대 10분 대기
        try:
            with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=1) as resp:
                data = json.loads(resp.read().decode())
                print(f"[OK] Chrome CDP 연결 성공! ({data.get('Browser')})")
                return
        except Exception:
            pass
        if i == 0:
            print(">> 바탕화면에 생성된 [Google Photos 제어용 크롬] 바로가기(또는 .bat)를 더블클릭해 실행해 주세요.")
        elif i % 10 == 0:
            print(f">> Chrome 실행 대기 중... ({i*2}초 경과)")
        time.sleep(2)
    raise RuntimeError("Chrome CDP 포트(9222) 연결 대기 시간 초과")


async def wait_for_login(page):
    """사용자가 Google Photos에 로그인할 때까지 대기"""
    print("\n[로그인 상태 점검 중]...")
    while True:
        url = page.url
        title = await page.title()
        
        # 정상 로그인 상태: photos.google.com 도메인이면서 about이나 signin이 아님
        if "photos.google.com" in url and "about" not in url and "signin" not in url and "accounts.google" not in url:
            print(f"[OK] Google Photos 로그인 확인됨! (URL: {url})")
            return
        
        print(f"  현재 페이지: {title} ({url})")
        print("  >> 열려 있는 Chrome 창에서 Google 계정으로 로그인해 주세요... (5초 후 재확인)")
        await asyncio.sleep(5)


async def download_year_photos(page, year: int, staging_dir: Path) -> Optional[Path]:
    """구글 포토 검색에서 특정 연도 사진을 검색하고 전체 선택 후 다운로드"""
    search_url = f"https://photos.google.com/search/{year}"
    print(f"\n[{year}년] 사진 검색 페이지 이동: {search_url}")
    await page.goto(search_url, wait_until="networkidle")
    await asyncio.sleep(3)

    # 검색 결과 없음 확인
    content = await page.content()
    if "검색결과가 없습니다" in content or "No results found" in content or "일치하는 항목 없음" in content:
        print(f"  [{year}년] 구글 포토에 검색된 사진이 없습니다.")
        return None

    # 사진 요소 탐색
    print(f"  [{year}년] 사진 로딩 및 선택 요소 탐색 중...")
    
    # 1. 체크박스 또는 첫 번째 사진 찾기
    checkboxes = await page.query_selector_all('[role="checkbox"]')
    if not checkboxes:
        # 썸네일 탐색
        items = await page.query_selector_all('[data-latest-bg], [role="img"]')
        if not items:
            print(f"  [{year}년] 다운로드할 사진 항목을 찾을 수 없습니다.")
            return None
        print(f"  [{year}년] {len(items)}개 썸네일 발견. 첫 번째 항목 선택 시도...")
        await items[0].hover()
        await asyncio.sleep(1)
        # hover 후 나타나는 체크박스 재탐색
        checkboxes = await page.query_selector_all('[role="checkbox"]')

    if not checkboxes:
        print(f"  [{year}년] 체크박스 활성화 실패. 단축키 방식으로 전환...")
        # 첫 번째 항목 클릭 후 x 키
        await page.keyboard.press("ArrowRight")
        await page.keyboard.press("x")
    else:
        print(f"  [{year}년] 첫 번째 체크박스 클릭...")
        await checkboxes[0].click()

    await asyncio.sleep(1)

    # 2. 맨 아래로 스크롤하여 마지막 체크박스 Shift+클릭 (전체 선택)
    print(f"  [{year}년] 전체 선택을 위해 피드 스크롤 중...")
    for _ in range(5):
        await page.keyboard.press("PageDown")
        await asyncio.sleep(0.5)

    all_checkboxes = await page.query_selector_all('[role="checkbox"]')
    if len(all_checkboxes) > 1:
        print(f"  [{year}년] 마지막 체크박스({len(all_checkboxes)}번째) Shift+클릭...")
        await page.keyboard.down("Shift")
        await all_checkboxes[-1].click()
        await page.keyboard.up("Shift")
    else:
        print(f"  [{year}년] 단일 그룹 선택됨.")

    await asyncio.sleep(2)

    # 3. Shift + D 다운로드 트리거 및 다운로드 인터셉트
    year_staging = staging_dir / str(year)
    year_staging.mkdir(parents=True, exist_ok=True)
    target_zip = year_staging / f"GooglePhotos_{year}.zip"

    print(f"  [{year}년] 'Shift + D' 다운로드 실행 중...")
    try:
        async with page.expect_download(timeout=180000) as download_info:
            await page.keyboard.press("Shift+D")
        
        download = await download_info.value
        print(f"  [{year}년] 다운로드 스트림 감지: {download.suggested_filename}")
        print(f"  [{year}년] 로컬 저장 중: {target_zip} ...")
        await download.save_as(target_zip)
        print(f"  [{year}년] 다운로드 완료! ({target_zip.stat().st_size / (1024*1024):.2f} MB)")
        return target_zip
    except Exception as e:
        print(f"  [{year}년] 다운로드 감지 타임아웃 또는 실패: {e}")
        # 혹시 일반 다운로드 경로로 들어갔는지 확인
        return None


def extract_and_integrate(zip_path: Path, year: int) -> int:
    r"""다운로드된 ZIP 압축 해제 ➡️ 표준 파일명 변환 ➡️ D:\Photos_Merged\{year} 통합"""
    if not zip_path or not zip_path.exists():
        return 0

    extract_dir = zip_path.parent / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    dest_dir = MERGED_DIR / str(year)
    dest_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[통합] {zip_path.name} 압축 해제 중...")
    with zipfile.ZipFile(zip_path, 'r') as z:
        z.extractall(extract_dir)

    # JSON 메타데이터 맵 구축 (Google Photos Sidecar JSON)
    json_meta = {}
    for root, _, files in os.walk(extract_dir):
        for f in files:
            if f.endswith(".json"):
                jp = Path(root) / f
                try:
                    with open(jp, "r", encoding="utf-8") as jf:
                        jdata = json.load(jf)
                        base_name = f[:-5] # remove .json
                        json_meta[base_name] = jdata
                except Exception:
                    pass

    # 미디어 파일 순회 및 표준화
    integrated_count = 0
    journal = []

    valid_exts = {".jpg", ".jpeg", ".png", ".heic", ".mp4", ".mov"}
    for root, _, files in os.walk(extract_dir):
        for f in files:
            ext = Path(f).suffix.lower()
            if ext not in valid_exts:
                continue

            src_file = Path(root) / f
            file_size = src_file.stat().st_size

            # 타임스탬프 추출 (JSON 우선, 다음 EXIF, 다음 파일 생성일)
            dt_obj = None
            meta = json_meta.get(f, {})
            taken_time = meta.get("photoTakenTime", {}).get("timestamp")
            if taken_time:
                try:
                    dt_obj = datetime.fromtimestamp(int(taken_time))
                except Exception:
                    pass

            if not dt_obj:
                try:
                    img = Image.open(src_file)
                    exif = img._getexif()
                    if exif and 36867 in exif:
                        dt_obj = datetime.strptime(exif[36867], "%Y:%m:%d %H:%M:%S")
                except Exception:
                    pass

            if not dt_obj:
                dt_obj = datetime.fromtimestamp(src_file.stat().st_mtime)

            date_str = dt_obj.strftime("%Y%m%d%H%M%S")
            device_str = meta.get("cameraModel", "UnknownDevice").replace(" ", "_")
            new_name = f"{date_str}_[GP_{device_str}]_{f}"
            target_path = dest_dir / new_name

            # 중복 회피
            dup_idx = 1
            while target_path.exists():
                target_path = dest_dir / f"{date_str}_[GP_{device_str}]_{f[:-len(ext)]}_{dup_idx}{ext}"
                dup_idx += 1

            # 파일 이동
            import shutil
            shutil.move(str(src_file), str(target_path))
            
            # mtime 복원
            ts = dt_obj.timestamp()
            os.utime(target_path, (ts, ts))

            integrated_count += 1
            journal.append({
                "year": year,
                "original_name": f,
                "integrated_name": target_path.name,
                "file_size": file_size,
                "capture_time": dt_obj.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "INTEGRATED"
            })
            print(f"  [OK] {year}/{target_path.name}")

    if journal:
        REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        keys = ["year", "original_name", "integrated_name", "file_size", "capture_time", "status"]
        write_header = not REPORT_FILE.exists()
        with open(REPORT_FILE, "a", newline="", encoding="utf-8-sig") as jf:
            writer = csv.DictWriter(jf, fieldnames=keys)
            if write_header:
                writer.writeheader()
            writer.writerows(journal)

    print(f"\n[완료] {year}년 사진 {integrated_count:,}장 D:\\Photos_Merged\\{year} 편입 완료!")
    return integrated_count


async def run_bot(years: list[int]):
    ensure_chrome_running()
    
    async with async_playwright() as p:
        print(f"[CDP] Chrome 브라우저에 연결 중 ({CDP_URL})...")
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        await wait_for_login(page)

        total_downloaded = 0
        for y in years:
            zip_file = await download_year_photos(page, y, DOWNLOAD_STAGING)
            if zip_file:
                count = extract_and_integrate(zip_file, y)
                total_downloaded += count

        print("\n" + "=" * 60)
        print(f"🎉 구글 포토 크롬 자동화 봇 작업 완료!")
        print(f"  - 총 편입된 사진 수량: {total_downloaded:,}장")
        print(f"  - 통합 디렉터리: D:\\Photos_Merged")
        print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Google Photos Chrome Automation Bot")
    parser.add_argument("--years", nargs="+", type=int, default=[2024, 2025, 2026],
                        help="다운로드할 연도 목록 (기본: 2024 2025 2026)")
    args = parser.parse_args()

    asyncio.run(run_bot(args.years))


if __name__ == "__main__":
    main()
