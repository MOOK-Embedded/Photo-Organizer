import asyncio
import csv
import json
import time
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

KEEP_TITLES = {
    '결혼한 해 2014',
    '쑥쑥이 엄마 뱃속에서 2015',
    '결혼 전 2013',
    'Recycled',
    '쑥쑥이 엄마 뱃속에서 2015_2',
    'Engineering',
    '의료비',
    '골프스윙',
    '쑥쑥이 초음파사진_모아베베',
    'AI OUTPUT',
    'Pictures\\출장 사진\\2009_06 폴란드 출장'
}

JOURNAL_FILE = Path(r"D:\_Reports\google_photos_album_deletion_journal.csv")
REMAINING_ALBUMS_CSV = Path(r"D:\_Reports\google_photos_remaining_albums.csv")

async def run_selective_deletion():
    print("==========================================================")
    print("  지정 11개 앨범 보존 및 잔여 16개 앨범 추가 삭제 작업    ")
    print("==========================================================")

    if not REMAINING_ALBUMS_CSV.exists():
        print(f"[ERROR] {REMAINING_ALBUMS_CSV} 파일이 없습니다!")
        return

    with open(REMAINING_ALBUMS_CSV, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    keep_list = [r for r in rows if r["Album_Title"] in KEEP_TITLES]
    delete_list = [r for r in rows if r["Album_Title"] not in KEEP_TITLES]

    print(f"보존 대상 앨범 ({len(keep_list)}개):")
    for k in keep_list:
        print(f"  [KEEP] {k['Album_Title']} ({k['Item_Count']})")

    print(f"\n삭제 대상 앨범 ({len(delete_list)}개):")
    for d in delete_list:
        print(f"  [DELETE] {d['Album_Title']} ({d['Item_Count']})")

    print("\nConnecting to Chrome via CDP port 9222...")
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = await ctx.new_page()

        deleted_count = 0
        failed_count = 0
        total_targets = len(delete_list)
        start_time = time.time()

        for idx, alb in enumerate(delete_list, 1):
            title = alb["Album_Title"]
            href = alb["Href"].replace("./album/", "/album/")
            count_str = alb["Item_Count"]
            target_url = f"https://photos.google.com{href}"

            if title in KEEP_TITLES:
                print(f"  [SAFETY SKIP] 보존 대상 감지: {title}")
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

                    # Wait for redirection
                    await asyncio.sleep(1.5)
                    success = True
                    status_str = "SUCCESS"
                    break

                except Exception as e:
                    note = str(e)[:100]
                    await asyncio.sleep(1.5)

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if success:
                deleted_count += 1
                with open(JOURNAL_FILE, "a", newline="", encoding="utf-8-sig") as jf:
                    writer = csv.writer(jf)
                    writer.writerow([now_str, title, count_str, href, status_str, note])
            else:
                failed_count += 1
                with open(JOURNAL_FILE, "a", newline="", encoding="utf-8-sig") as jf:
                    writer = csv.writer(jf)
                    writer.writerow([now_str, title, count_str, href, "FAILED", note])

            print(f"[{idx}/{total_targets}] {status_str}: {title} ({count_str})")

        print("\n" + "=" * 60)
        print("  16개 지정 앨범 삭제 완료!")
        print(f"  - 삭제 완료 : {deleted_count}개")
        print(f"  - 실패 : {failed_count}개")
        print("=" * 60)

        # -------------------------------------------------------------
        # FINAL VERIFICATION (최종 전수 재검증)
        # -------------------------------------------------------------
        print("\n[최종 검증] 남아있는 앨범 및 사진 라이브러리 전수 재검증...")
        await page.goto("https://photos.google.com/albums", wait_until="domcontentloaded", timeout=25000)
        await asyncio.sleep(3)

        final_albums = set()
        for _ in range(10):
            titles = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('a[href*="album/"]'))
                    .map(a => (a.innerText || '').split('\\n')[0].trim())
                    .filter(Boolean);
            }""")
            for t in titles:
                final_albums.add(t)
            await page.keyboard.press("PageDown")
            await asyncio.sleep(0.5)

        print(f"\n최종 남아있는 앨범 ({len(final_albums)}개):")
        for a in sorted(list(final_albums)):
            print(f"  - {a}")

        # Check Trash
        await page.goto("https://photos.google.com/trash", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
        trash_cnt = await page.evaluate("() => document.querySelectorAll('a[href*=\"photo/\"]').length")
        print(f"\n휴지통 사진 수: {trash_cnt}개 (0개 정상)")

        # Check Storage Quota
        await page.goto("https://photos.google.com/quotamanagement", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
        quota_text = await page.evaluate("() => document.body.innerText")
        for line in quota_text.split("\n"):
            if "Google" in line and "GB" in line:
                print(f"스토리지 현황: {line.strip()}")

        # Save final report
        final_report_file = Path(r"D:\_Reports\google_photos_final_album_cleanup_verification.json")
        with open(final_report_file, "w", encoding="utf-8") as vf:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "final_album_count": len(final_albums),
                "remaining_albums": sorted(list(final_albums)),
                "trash_photo_count": trash_cnt,
                "verification_status": "ALL_PHOTOS_PRESERVED" if trash_cnt == 0 else "WARNING_TRASH_NOT_ZERO"
            }, vf, ensure_ascii=False, indent=2)

        print(f"\n[저장 완료] 최종 검증 리포트: {final_report_file}")
        await page.close()

if __name__ == "__main__":
    asyncio.run(run_selective_deletion())
