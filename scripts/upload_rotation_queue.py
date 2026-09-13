import argparse
import asyncio
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

DB_PATH = Path(r"D:\_Reports\rotation_upload_progress.db")
DEFAULT_BATCH_SIZE = 3

def print_stats():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    cur.execute("SELECT status, count(*) FROM upload_queue GROUP BY status")
    status_counts = dict(cur.fetchall())
    total = sum(status_counts.values())
    pending = status_counts.get("pending", 0)
    success = status_counts.get("success", 0)
    failed = status_counts.get("failed", 0)
    skipped = status_counts.get("skipped", 0)
    
    print("\n" + "=" * 65)
    print("      Google Photos 2014~2026 회전 보정본 업로드 상태 리포트      ")
    print("=" * 65)
    print(f"  전체 대상: {total:,} 장")
    print(f"  완료됨:    {success:,} 장 ({success/total*100:5.2f}%)" if total else "")
    print(f"  대기중:    {pending:,} 장 ({pending/total*100:5.2f}%)" if total else "")
    print(f"  실패:      {failed:,} 장 ({failed/total*100:5.2f}%)" if total else "")
    print(f"  스킵:      {skipped:,} 장 ({skipped/total*100:5.2f}%)" if total else "")
    print("-" * 65)
    print(f"{"연도":<6} | {"전체":>8} | {"완료":>8} | {"대기":>8} | {"실패":>6} | {"진행률":>8}")
    print("-" * 65)
    
    cur.execute("""
        SELECT year,
               count(*) as total,
               sum(CASE WHEN status="success" THEN 1 ELSE 0 END) as success,
               sum(CASE WHEN status="pending" THEN 1 ELSE 0 END) as pending,
               sum(CASE WHEN status="failed" THEN 1 ELSE 0 END) as failed
        FROM upload_queue
        GROUP BY year
        ORDER BY year ASC
    """)
    for y, tot, succ, pend, fail in cur.fetchall():
        pct = (succ / tot * 100) if tot else 0
        print(f"{y:<6} | {tot:>8,} | {succ:>8,} | {pend:>8,} | {fail:>6,} | {pct:>7.1f}%")
    print("=" * 65 + "\n")
    conn.close()

async def upload_batch(page, batch_files):
    # 1. Ensure clean Google Photos page
    if "photos.google.com" not in page.url or "/search/" in page.url:
        await page.goto("https://photos.google.com", wait_until="domcontentloaded")
        await asyncio.sleep(2.0)

    # Check if menu already open
    is_open = await page.evaluate("""() => {
        const menu = document.querySelector('ul[role="menu"]');
        return menu !== null && menu.offsetParent !== null;
    }""")

    if not is_open:
        btn_center = await page.evaluate("""() => {
            const b = document.querySelector('button[aria-label*="만들기 및 추가"]');
            if (b) {
                const r = b.getBoundingClientRect();
                return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
            }
            return null;
        }""")
        if not btn_center:
            raise Exception("'+' 버튼 찾기 실패")
        await page.mouse.click(btn_center['x'], btn_center['y'])
        await asyncio.sleep(1.0)

    # 2. Get '사진 가져오기' center
    import_center = await page.evaluate("""() => {
        const items = Array.from(document.querySelectorAll('li[role="menuitem"], [role="menuitem"], li, div'));
        const target = items.find(el => (el.innerText || '').trim() === '사진 가져오기' && el.offsetParent !== null);
        if (target) {
            const r = target.getBoundingClientRect();
            return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
        }
        return null;
    }""")
    if not import_center:
        raise Exception("'사진 가져오기' 메뉴 찾기 실패")

    # 3. Intercept file chooser and click
    async with page.expect_file_chooser(timeout=15000) as fc_info:
        await page.mouse.click(import_center['x'], import_center['y'])

    fc = await fc_info.value
    await fc.set_files(batch_files, timeout=120000)
    await asyncio.sleep(1.2)

    # 4. Handle quality dialog if present
    await page.evaluate("""() => {
        const dialog = document.querySelector('div[role="dialog"]');
        if (!dialog) return false;
        const btns = Array.from(dialog.querySelectorAll('button'));
        const continueBtn = btns.find(b => b.innerText.trim() === '계속' || b.innerText.trim() === 'Continue');
        if (continueBtn) { continueBtn.click(); return true; }
        return false;
    }""")

    # 5. Wait for upload completion toast
    success = False
    seen_uploading = False
    for s in range(1, 45):
        await asyncio.sleep(1)
        toasts = await page.evaluate("""() => {
            const nodes = Array.from(document.querySelectorAll('div[role="alert"], div[role="status"], div[role="dialog"], div[aria-live]'));
            return nodes.map(n => (n.innerText || '').trim().replace(/\\n+/g, ' ')).filter(Boolean);
        }""")
        status_text = " | ".join(dict.fromkeys(toasts))
        if "업로드 중" in status_text or "Uploading" in status_text:
            seen_uploading = True

        if any(k in status_text for k in ["업로드되었습니다", "업로드 완료", "업로드됨", "Upload complete", "uploaded", "앨범에 추가"]):
            if "업로드 중" not in status_text:
                success = True
                break
        elif seen_uploading and "업로드 중" not in status_text:
            success = True
            break
        elif s > 12 and not status_text:
            success = True
            break

    return success

async def process_queue(year=None, batch_size=DEFAULT_BATCH_SIZE, limit=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    query = "SELECT filepath FROM upload_queue WHERE status = 'pending'"
    params = []
    if year:
        query += " AND year = ?"
        params.append(year)
    query += " ORDER BY year ASC, filepath ASC"
    if limit:
        query += f" LIMIT {int(limit)}"

    cur.execute(query, params)
    rows = [r[0] for r in cur.fetchall()]
    conn.close()

    if not rows:
        label = str(year) if year else "전체"
        print(f"[알림] 처리할 대기 파일이 없습니다. (연도: {label})")
        return

    total_files = len(rows)
    batches = [rows[i:i + batch_size] for i in range(0, total_files, batch_size)]
    yr_label = str(year) if year else "2014~2026 전체"
    print(f"\n[업로드 시작] 연도: {yr_label} | 대상: {total_files:,}장 | 총 {len(batches):,}개 배치 (배치당 {batch_size}장)")

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        for pg in ctx.pages[1:]:
            await pg.close()
        page = ctx.pages[0]

        start_time = time.time()
        success_count = 0
        fail_count = 0

        for b_idx, b_files in enumerate(batches, 1):
            batch_success = False
            last_err = ""
            for attempt in range(1, 4):
                try:
                    batch_success = await upload_batch(page, b_files)
                    if batch_success:
                        break
                except Exception as e:
                    last_err = str(e)
                    if attempt < 3:
                        await asyncio.sleep(2)

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()

            if batch_success:
                success_count += len(b_files)
                cur.executemany(
                    "UPDATE upload_queue SET status='success', uploaded_at=?, batch_idx=?, error_msg=NULL WHERE filepath=?",
                    [(now_str, b_idx, f) for f in b_files]
                )
            else:
                fail_count += len(b_files)
                cur.executemany(
                    "UPDATE upload_queue SET status='failed', uploaded_at=?, batch_idx=?, error_msg=? WHERE filepath=?",
                    [(now_str, b_idx, last_err, f) for f in b_files]
                )

            conn.commit()
            conn.close()

            elapsed = time.time() - start_time
            rate = success_count / elapsed if elapsed > 0 else 0
            eta_sec = (total_files - success_count) / rate if rate > 0 else 0
            eta_min = eta_sec / 60

            pct = success_count / total_files * 100
            msg = f"[{b_idx}/{len(batches)}] {success_count:,}/{total_files:,} 완료 ({pct:5.1f}%) | 속도: {rate*60:4.0f}장/분 | ETA: {eta_min:5.1f}분"
            if b_idx % 5 == 0 or b_idx == len(batches):
                print(msg, flush=True)
            else:
                print(f"\r{msg}", end="", flush=True)

        print(f"\n\n[작업 완료] 성공: {success_count:,}장, 실패: {fail_count:,}장, 소요시간: {(time.time()-start_time)/60:.1f}분")

def main():
    parser = argparse.ArgumentParser(description="Google Photos 2014+ 회전 보정본 배치 업로더")
    parser.add_argument("--year", type=int, help="특정 연도만 처리 (예: 2014)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="배치당 파일 수 (기본: 3)")
    parser.add_argument("--limit", type=int, help="최대 처리 파일 수 제한 (테스트용)")
    parser.add_argument("--stats", action="store_true", help="현재 진행 통계 출력")
    args = parser.parse_args()

    if args.stats:
        print_stats()
        return

    asyncio.run(process_queue(year=args.year, batch_size=args.batch_size, limit=args.limit))

if __name__ == "__main__":
    main()
