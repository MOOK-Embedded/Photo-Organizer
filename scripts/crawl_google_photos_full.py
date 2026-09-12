import asyncio
import json
import csv
import re
from pathlib import Path
from playwright.async_api import async_playwright

async def deep_crawl_google_photos():
    print("==================================================")
    print("  Google Photos Full Deep Crawler (CDP Port 9222)  ")
    print("==================================================")

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = await ctx.new_page()

        # -------------------------------------------------------------
        # STEP 1: CRAWL ALL ALBUMS (Full virtualization-aware scroll)
        # -------------------------------------------------------------
        print("\n[Step 1] Navigating to https://photos.google.com/albums ...")
        await page.goto("https://photos.google.com/albums", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        albums_dict = {}
        last_new_at_step = 0
        total_steps = 250  # Enough to cover 400+ albums

        print("Scrolling through all albums with PageDown...")
        for step in range(1, total_steps + 1):
            # Extract currently visible albums
            extracted = await page.evaluate("""() => {
                const results = [];
                const cards = document.querySelectorAll('a[href*="./album/"], a[href*="/album/"]');
                for (const c of cards) {
                    const href = c.getAttribute('href');
                    if (!href) continue;
                    
                    const textLines = (c.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                    let title = '';
                    let countStr = '';
                    if (textLines.length >= 2) {
                        title = textLines[0];
                        countStr = textLines[1];
                    } else if (textLines.length === 1) {
                        title = textLines[0];
                    }
                    const aria = c.getAttribute('aria-label') || '';
                    results.push({
                        href: href,
                        title: title || aria,
                        countStr: countStr,
                        rawText: textLines.join(' | ')
                    });
                }
                return results;
            }""")

            new_in_step = 0
            for item in extracted:
                h = item["href"]
                if h not in albums_dict:
                    albums_dict[h] = item
                    new_in_step += 1

            if new_in_step > 0:
                last_new_at_step = step
                if len(albums_dict) % 20 == 0 or new_in_step > 5:
                    print(f"  Step {step}: Total collected so far = {len(albums_dict)} albums (+{new_in_step})")

            # Check if reached bottom (no new albums in 12 consecutive steps)
            if step - last_new_at_step >= 12 and len(albums_dict) > 0:
                print(f"  Reached bottom at step {step}. Total unique albums: {len(albums_dict)}")
                break

            await page.keyboard.press("PageDown")
            await asyncio.sleep(0.6)

        album_list = list(albums_dict.values())
        print(f"==> Total Albums Collected: {len(album_list)}")

        # -------------------------------------------------------------
        # STEP 2: CRAWL ARCHIVE (보관함 - https://photos.google.com/archive)
        # -------------------------------------------------------------
        print("\n[Step 2] Navigating to https://photos.google.com/archive ...")
        await page.goto("https://photos.google.com/archive", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        archive_dates = set()
        archive_photos_count = 0
        archive_samples = []

        print("Scrolling through Archive...")
        for a_step in range(1, 40):
            arch_data = await page.evaluate("""() => {
                const photos = document.querySelectorAll('a[href*="photo/"]');
                const headings = Array.from(document.querySelectorAll('h2, [role="heading"], div[aria-label]'))
                    .map(el => el.innerText || el.getAttribute('aria-label') || '')
                    .filter(t => /\\d{4}년/.test(t));
                
                const sampleHrefs = [];
                for (let i = 0; i < Math.min(photos.length, 5); i++) {
                    sampleHrefs.push(photos[i].getAttribute('href') || '');
                }
                return {
                    count: photos.length,
                    headings: headings,
                    samples: sampleHrefs
                };
            }""")
            archive_photos_count = max(archive_photos_count, arch_data["count"])
            for h in arch_data["headings"]:
                archive_dates.add(h)
            for s in arch_data["samples"]:
                if s not in archive_samples:
                    archive_samples.append(s)

            await page.keyboard.press("PageDown")
            await asyncio.sleep(0.5)

        print(f"==> Archive Photos detected: ~{archive_photos_count} visible")
        print(f"==> Archive Dates span: {sorted(list(archive_dates))[:15]}")

        # -------------------------------------------------------------
        # STEP 3: INSPECT MAIN PHOTOS FEED DATES (https://photos.google.com/)
        # -------------------------------------------------------------
        print("\n[Step 3] Navigating to Main Feed https://photos.google.com/ ...")
        await page.goto("https://photos.google.com/", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        feed_dates = set()
        print("Sampling feed dates from top and recent...")
        for f_step in range(1, 25):
            dates = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('h2, [role="heading"], div[aria-label]'))
                    .map(el => el.innerText || el.getAttribute('aria-label') || '')
                    .filter(t => /\\d{4}년/.test(t));
            }""")
            for d in dates:
                feed_dates.add(d)
            await page.keyboard.press("PageDown")
            await asyncio.sleep(0.5)

        print(f"==> Feed Dates sampled: {sorted(list(feed_dates), reverse=True)[:15]}")

        # -------------------------------------------------------------
        # STEP 4: SAVE COMPREHENSIVE OUTPUT
        # -------------------------------------------------------------
        reports_dir = Path(r"D:\_Reports")
        reports_dir.mkdir(parents=True, exist_ok=True)

        out_json = reports_dir / "google_photos_deep_crawl.json"
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump({
                "albums_count": len(album_list),
                "albums": album_list,
                "archive": {
                    "estimated_count": archive_photos_count,
                    "dates": sorted(list(archive_dates)),
                    "samples": archive_samples[:20]
                },
                "feed_recent_dates": sorted(list(feed_dates), reverse=True)
            }, f, ensure_ascii=False, indent=2)

        out_csv = reports_dir / "google_photos_cloud_all_albums.csv"
        with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["Index", "Album_Title", "Item_Count_Str", "Item_Count_Num", "Href"])
            for idx, alb in enumerate(album_list, 1):
                cnt_str = alb.get("countStr", "")
                num_match = re.search(r"(\d+)", cnt_str.replace(",", ""))
                num = int(num_match.group(1)) if num_match else 0
                writer.writerow([idx, alb.get("title", ""), cnt_str, num, alb.get("href", "")])

        print(f"\nSaved deep crawl results to:\n  - {out_json}\n  - {out_csv}")
        await page.close()

if __name__ == "__main__":
    asyncio.run(deep_crawl_google_photos())
