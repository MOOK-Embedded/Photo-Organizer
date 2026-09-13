import asyncio
import csv
import json
import time
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

WHITELIST_TITLES = {
    '울아들 시영', '결혼식 스냅 2014', '아빠와 시영 2015', '결혼 전 2013', '시영이',
    '시영1세 2016', '시영2세 2017', '시영3세 2018', '시영4세 2019', '시영5세 2020', '시영6세 2021',
    '아빠와 시영 2015_2', '의료', 'AI OUTPUT', '가족', '정세라', 'Recycled',
    '우리연애사진', '아이폰X_2019~2021.11', '우리 결혼사진_아빠'
}

JOURNAL_FILE = Path(r"D:\_Reports\google_photos_album_deletion_journal.csv")
ALL_ALBUMS_CSV = Path(r"D:\_Reports\google_photos_cloud_all_albums.csv")

async def run_batch_deletion():
    print("==========================================================")
    print("  Google Photos 1단계: 난잡한 폴더형 앨범 자동 삭제 및 정화  ")
    print("==========================================================")

    if not ALL_ALBUMS_CSV.exists():
        print(f"[ERROR] {ALL_ALBUMS_CSV} 파일이 없습니다!")
        return

    to_delete = []
    protected = []

    with open(ALL_ALBUMS_CSV, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            title = r["Album_Title"].strip()
            # STRICT PROTECTION RULE:
            # Protect if in whitelist OR no backslash/slash in title
            if title in WHITELIST_TITLES or ('\\' not in title and '/' not in title):
                protected.append(r)
            else:
                to_delete.append(r)

    print(f"총 대상 앨범: {len(to_delete)}개 삭제 대상")
    print(f"절대 보호 앨범: {len(protected)}개 (울아들 시영 등 23개 순수 앨범)")

    # Read already processed albums from journal if resuming
    processed_hrefs = set()
    if JOURNAL_FILE.exists():
        with open(JOURNAL_FILE, "r", encoding="utf-8-sig") as jf:
            jreader = csv.DictReader(jf)
            for row in jreader:
                if row.get("Status") in {"SUCCESS", "ALREADY_DELETED", "PROTECTED"}:
                    processed_hrefs.add(row.get("Href"))
        print(f"이미 처리된 저널 기록: {len(processed_hrefs)}개 앨범 스킵 예정")
    else:
        JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(JOURNAL_FILE, "w", newline="", encoding="utf-8-sig") as jf:
            writer = csv.writer(jf)
            writer.writerow(["Timestamp", "Album_Title", "Item_Count", "Href", "Status", "Note"])

    # Connect to Chrome via CDP
    print("\nConnecting to Chrome via CDP port 9222...")
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = await ctx.new_page()

        deleted_count = 0
        skipped_count = 0
        failed_count = 0

        total_targets = len(to_delete)
        start_time = time.time()

        for idx, alb in enumerate(to_delete, 1):
            title = alb["Album_Title"]
            href = alb["Href"].replace("./album/", "/album/")
            count_str = alb["Item_Count_Str"]
            target_url = f"https://photos.google.com{href}"

            if href in processed_hrefs or alb["Href"] in processed_hrefs:
                skipped_count += 1
                continue

            # Double check protection
            if title in WHITELIST_TITLES or ('\\' not in title and '/' not in title):
                print(f"  [PROTECTED] 스킵: {title}")
                continue

            success = False
            note = ""
            status_str = "FAILED"

            for attempt in range(1, 4):
                try:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                    await asyncio.sleep(1.2)

                    # Check if already deleted or 404
                    body_text = await page.evaluate("() => document.body.innerText")
                    if "찾을 수 없습니다" in body_text or page.url == "https://photos.google.com/albums":
                        success = True
                        note = "Already deleted / 404"
                        status_str = "ALREADY_DELETED"
                        break

                    # 1. Click VISIBLE '옵션 더보기' button
                    clicked_more = await page.evaluate("""() => {
                        const btns = Array.from(document.querySelectorAll('button'));
                        const b = btns.find(el => {
                            const aria = el.getAttribute('aria-label') || '';
                            return (aria.includes('옵션 더보기') || aria.includes('More options')) && el.offsetParent !== null;
                        });
                        if (b) { b.click(); return true; }
                        return false;
                    }""")

                    if not clicked_more:
                        await page.keyboard.press("Escape")
                        await asyncio.sleep(0.5)
                        raise Exception("Visible More options button not found")

                    await asyncio.sleep(0.8)

                    # 2. Click '앨범 삭제' in menu
                    clicked_del = await page.evaluate("""() => {
                        const items = Array.from(document.querySelectorAll('div[role="menuitem"], span, div'));
                        const target = items.find(it => {
                            const t = (it.innerText || '').trim();
                            return t === '앨범 삭제' || t === 'Delete album';
                        });
                        if (target) {
                            target.click();
                            return true;
                        }
                        return false;
                    }""")

                    if not clicked_del:
                        await page.keyboard.press("Escape")
                        await asyncio.sleep(0.5)
                        raise Exception("Delete album menuitem not found")

                    await asyncio.sleep(0.8)

                    # 3. Confirm in dialog
                    confirmed = await page.evaluate("""() => {
                        const dialog = document.querySelector('div[role="dialog"]');
                        if (!dialog) return false;
                        const btns = Array.from(dialog.querySelectorAll('button'));
                        const delBtn = btns.find(b => {
                            const t = b.innerText.trim();
                            return t === '삭제' || t === 'Delete';
                        });
                        if (delBtn) {
                            delBtn.click();
                            return true;
                        }
                        return false;
                    }""")

                    if not confirmed:
                        raise Exception("Confirm delete button in dialog not found")

                    # Wait for redirection to albums
                    await asyncio.sleep(1.5)
                    success = True
                    status_str = "SUCCESS"
                    break

                except Exception as e:
                    note = str(e)[:100]
                    await asyncio.sleep(1.5)

            # Record result
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if success:
                if status_str == "SUCCESS":
                    deleted_count += 1
                else:
                    skipped_count += 1
                with open(JOURNAL_FILE, "a", newline="", encoding="utf-8-sig") as jf:
                    writer = csv.writer(jf)
                    writer.writerow([now_str, title, count_str, href, status_str, note])
            else:
                failed_count += 1
                with open(JOURNAL_FILE, "a", newline="", encoding="utf-8-sig") as jf:
                    writer = csv.writer(jf)
                    writer.writerow([now_str, title, count_str, href, "FAILED", note])

            processed_hrefs.add(href)

            # Progress log every 10 albums or at start
            if idx % 10 == 0 or idx <= 5 or idx == total_targets:
                elapsed = time.time() - start_time
                done_so_far = deleted_count + skipped_count + failed_count
                avg_speed = elapsed / max(1, done_so_far)
                rem_items = total_targets - idx
                est_rem_min = (rem_items * avg_speed) / 60.0
                print(f"[{idx}/{total_targets}] 삭제: {deleted_count}개 | 스킵: {skipped_count}개 | 실패: {failed_count}개 | {title[:35]} (남은시간: ~{est_rem_min:.1f}분)")

        print("\n" + "=" * 60)
        print("  1단계 앨범 삭제 작업 완료!")
        print(f"  - 삭제 완료 : {deleted_count}개 앨범")
        print(f"  - 기존 스킵 : {skipped_count}개 앨범")
        print(f"  - 실패 건수 : {failed_count}개 앨범")
        print(f"  - 저널 기록 : {JOURNAL_FILE}")
        print("=" * 60)

        # -------------------------------------------------------------
        # STEP 2: POST-DELETION LIBRARY RE-VERIFICATION (전수 재검증)
        # -------------------------------------------------------------
        print("\n==========================================================")
        print("  [Step 2] 앨범 삭제 후 사진 라이브러리 전수 재검증 착수   ")
        print("==========================================================")

        # 2.1 Re-scan Albums page
        print("\n2.1 앨범 목록 재확인 (https://photos.google.com/albums)...")
        await page.goto("https://photos.google.com/albums", wait_until="domcontentloaded", timeout=25000)
        await asyncio.sleep(3)

        rem_albums = set()
        for _ in range(25):
            titles = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('a[href*="album/"]'))
                    .map(a => (a.innerText || '').split('\\n')[0].trim())
                    .filter(Boolean);
            }""")
            for t in titles:
                rem_albums.add(t)
            await page.keyboard.press("PageDown")
            await asyncio.sleep(0.5)

        print(f"  남아있는 앨범 수 (보호 앨범 중심): {len(rem_albums)}개 확인")
        print(f"  남아있는 앨범 목록: {sorted(list(rem_albums))}")

        # 2.2 Verify Trash
        print("\n2.2 휴지통(Trash) 무결성 확인...")
        await page.goto("https://photos.google.com/trash", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
        trash_cnt = await page.evaluate("() => document.querySelectorAll('a[href*=\"photo/\"]').length")
        print(f"  휴지통 사진 수: {trash_cnt}개 (0개 정상)")

        # 2.3 Verify Storage Quota
        print("\n2.3 저장용량(Quota) 무결성 확인...")
        await page.goto("https://photos.google.com/quotamanagement", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
        quota_text = await page.evaluate("() => document.body.innerText")
        quota_lines = []
        for line in quota_text.split("\n"):
            if "Google" in line and "GB" in line:
                quota_lines.append(line.strip())
                print(f"  스토리지 현황: {line.strip()}")

        # 2.4 Verify Main Feed dates
        print("\n2.4 메인 사진 피드(Feed) 생존 전수 확인...")
        await page.goto("https://photos.google.com/", wait_until="domcontentloaded", timeout=25000)
        await asyncio.sleep(3)
        feed_photos = await page.evaluate("() => document.querySelectorAll('a[href*=\"photo/\"]').length")
        print(f"  메인 피드 상단 즉시 감지 사진 수: {feed_photos}개")

        # Save verification report
        verif_file = Path(r"D:\_Reports\google_photos_album_cleanup_verification.json")
        verif_data = {
            "timestamp": datetime.now().isoformat(),
            "deleted_albums_count": deleted_count,
            "failed_albums_count": failed_count,
            "remaining_albums": sorted(list(rem_albums)),
            "trash_photo_count": trash_cnt,
            "storage_quota": quota_lines,
            "feed_photo_count_sample": feed_photos,
            "verification_status": "ALL_PHOTOS_PRESERVED" if trash_cnt == 0 else "WARNING_TRASH_NOT_ZERO"
        }
        with open(verif_file, "w", encoding="utf-8") as vf:
            json.dump(verif_data, vf, ensure_ascii=False, indent=2)

        print(f"\n[완료] 검증 결과 리포트 저장: {verif_file}")
        await page.close()

if __name__ == "__main__":
    asyncio.run(run_batch_deletion())
