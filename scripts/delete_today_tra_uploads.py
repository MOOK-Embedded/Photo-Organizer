import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

BEFORE_PNG = Path(r"D:\photos_uploader\reports\tra_before_delete.png")
AFTER_PNG = Path(r"D:\photos_uploader\reports\tra_after_delete.png")
TRASH_PNG = Path(r"D:\photos_uploader\reports\trash_after_delete.png")
SUMMARY_JSON = Path(r"D:\_Reports\delete_today_tra_summary.json")

RECENT_URL = "https://photos.google.com/search/ChfstZzqt7wg7LaU6rCA65CcIO2VreuqqSIIEgYKBHICCgAogIWh2Ik0"

async def delete_today_uploads():
    print("==========================================================")
    print("  Google Photos 오늘 업로드된 회전 결함 사진 휴지통 이동   ")
    print("==========================================================")

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        print("\n1. Navigating to Recent Uploads page...")
        if RECENT_URL not in page.url:
            await page.goto(RECENT_URL, wait_until="commit", timeout=20000)
            await asyncio.sleep(2)

        # Submit search query
        await page.keyboard.press("Enter")
        await asyncio.sleep(4)

        # Before screenshot
        await page.screenshot(path=str(BEFORE_PNG))
        print(f"   -> Before screenshot saved: {BEFORE_PNG}")

        batches_processed = 0
        start_time = time.time()

        for batch_idx in range(1, 10):
            # 1. Check if '오늘 추가됨' H2 exists
            h2 = await page.evaluate("""() => {
                const h = Array.from(document.querySelectorAll('h2')).find(el => (el.innerText || '').includes('오늘'));
                if (h) {
                    const rect = h.getBoundingClientRect();
                    return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, text: h.innerText.trim() };
                }
                return null;
            }""")

            if not h2:
                print("\n   -> '오늘 추가됨' 섹션이 없습니다. 모든 배치 정리 완료!")
                break

            print(f"\n[배치 {batch_idx}] '오늘 추가됨' 발견! 마우스 호버 이동 ({h2['x']:.0f}, {h2['y']:.0f})...")
            await page.mouse.move(h2['x'], h2['y'])
            await asyncio.sleep(1.0)

            # 2. Check if checkbox appeared and get its center
            cb = await page.evaluate("""() => {
                const el = document.querySelector('div.QcpS9c.R4HkWb');
                if (el) {
                    const rect = el.getBoundingClientRect();
                    return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
                }
                return null;
            }""")

            if not cb:
                print("   [경고] 체크박스가 나타나지 않았습니다. 재시도...")
                await asyncio.sleep(2)
                continue

            # 3. Click checkbox
            print(f"   -> 체크박스 클릭 ({cb['x']:.0f}, {cb['y']:.0f})...")
            await page.mouse.click(cb['x'], cb['y'])
            await asyncio.sleep(2.0)

            # 4. Check selection count
            sel_str = await page.evaluate("""() => {
                const el = Array.from(document.querySelectorAll('*')).find(e =>
                    e.children.length === 0 && (e.innerText || '').includes('선택함')
                );
                return el ? el.innerText.trim() : '알 수 없음';
            }""")
            print(f"   -> 선택 완료: {sel_str}")

            # 5. Click Trash button
            clicked_trash = await page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button, div[role="button"]'));
                const b = btns.find(el => (el.getAttribute('aria-label') || '').includes('휴지통') && el.offsetParent !== null);
                if (b) { b.click(); return true; }
                return false;
            }""")

            if not clicked_trash:
                print("   [오류] 상단 툴바 휴지통 버튼 클릭 실패!")
                await page.keyboard.press("Escape")
                break

            await asyncio.sleep(2.0)

            # 6. Confirm in dialog
            confirmed = await page.evaluate("""() => {
                const dialog = document.querySelector('div[role="dialog"]');
                if (!dialog) return false;
                const btns = Array.from(dialog.querySelectorAll('button'));
                if (btns.length > 0) {
                    btns[btns.length - 1].click();
                    return true;
                }
                return false;
            }""")

            if not confirmed:
                print("   [오류] 확인 다이얼로그 버튼 클릭 실패!")
                await page.keyboard.press("Escape")
                break

            print("   -> '휴지통으로 이동' 확인 완료! 6초 대기...")
            batches_processed += 1
            await asyncio.sleep(6.0)

            # 7. Press Enter to refresh search view
            await page.keyboard.press("Enter")
            await asyncio.sleep(4.0)

        # After screenshot
        print("\n2. 최종 상태 확인 중...")
        await page.screenshot(path=str(AFTER_PNG))
        print(f"   -> After screenshot saved: {AFTER_PNG}")

        # Check Trash
        print("\n3. 휴지통 상태 확인 중...")
        await page.goto("https://photos.google.com/trash", wait_until="commit", timeout=20000)
        await asyncio.sleep(4)
        await page.screenshot(path=str(TRASH_PNG))
        print(f"   -> Trash screenshot saved: {TRASH_PNG}")

        trash_items_count = await page.evaluate("""() => {
            return document.querySelectorAll('a[href*="photo/"]').length;
        }""")
        print(f"   -> 휴지통 내 감지된 항목 수: {trash_items_count}")

        elapsed = time.time() - start_time
        summary = {
            "timestamp": datetime.now().isoformat(),
            "batches_processed": batches_processed,
            "elapsed_seconds": round(elapsed, 1),
            "trash_items_count": trash_items_count,
            "status": "COMPLETED"
        }
        with open(SUMMARY_JSON, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        print("\n" + "=" * 60)
        print(f"  정리 완료! 총 {batches_processed}개 배치 처리 ({elapsed:.1f}초)")
        print(f"  휴지통 확인: {trash_items_count}개 항목")
        print(f"  리포트 저장: {SUMMARY_JSON}")
        print("=" * 60)

if __name__ == "__main__":
    asyncio.run(delete_today_uploads())
