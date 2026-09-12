import os
import sys
import json
import csv
import zipfile
import shutil
import asyncio
from datetime import datetime
from pathlib import Path
from PIL import Image
from playwright.async_api import async_playwright

STAGING_DIR = Path(r"D:\_GooglePhotos_Recent_Downloads\2024")
DEST_DIR = Path(r"D:\Photos_Merged\2024")
REPORT_FILE = Path(r"D:\_Reports\chrome_bot_download_journal.csv")

async def download_2024(target_zip: Path):
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        page = browser.contexts[0].pages[0]

        print("[2024] Navigating to https://photos.google.com/search/2024 ...")
        await page.goto("https://photos.google.com/search/2024", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(4)

        # Hover over first photo to activate checkboxes
        first_item = await page.query_selector('a[href*="photo/"]')
        if not first_item:
            print("[2024] No photos found on page!")
            return False

        print("[2024] Hovering over first photo to activate checkboxes...")
        await first_item.hover()
        await asyncio.sleep(1)

        cbs = await page.query_selector_all('[role="checkbox"]')
        print(f"[2024] Active checkboxes: {len(cbs)}")
        if not cbs:
            print("[2024] Checkboxes still not visible!")
            return False

        # Click the group header checkbox
        print(f"[2024] Clicking header checkbox: {await cbs[0].get_attribute('aria-label')}")
        await cbs[0].evaluate("el => el.click()")
        await asyncio.sleep(2)

        print("[2024] Triggering Shift+D download...")
        async with page.expect_download(timeout=180000) as dl_info:
            await page.keyboard.press("Shift+D")
        dl = await dl_info.value
        print(f"[2024] Download stream started: {dl.suggested_filename}")
        await dl.save_as(target_zip)
        print(f"[2024] Download finished! Size: {target_zip.stat().st_size / (1024*1024):.2f} MB")
        return True

def integrate_2024(zip_path: Path):
    if not zip_path.exists():
        print(f"[ERROR] {zip_path} not found!")
        return 0

    extract_dir = STAGING_DIR / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    DEST_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n[통합] {zip_path.name} 압축 해제 중...")
    with zipfile.ZipFile(zip_path, 'r') as z:
        z.extractall(extract_dir)

    json_meta = {}
    for root, _, files in os.walk(extract_dir):
        for f in files:
            if f.endswith(".json"):
                jp = Path(root) / f
                try:
                    with open(jp, "r", encoding="utf-8") as jf:
                        json_meta[f[:-5]] = json.load(jf)
                except Exception:
                    pass

    valid_exts = {".jpg", ".jpeg", ".png", ".heic", ".mp4", ".mov", ".dng", ".webp"}
    integrated = 0
    skipped = 0
    journal = []

    for root, _, files in os.walk(extract_dir):
        for f in files:
            ext = Path(f).suffix.lower()
            if ext not in valid_exts:
                continue

            src = Path(root) / f
            file_size = src.stat().st_size

            meta = json_meta.get(f, {})
            dt_obj = None
            taken_ts = meta.get("photoTakenTime", {}).get("timestamp")
            if taken_ts:
                try:
                    dt_obj = datetime.fromtimestamp(int(taken_ts))
                except Exception:
                    pass

            res_str = ""
            if ext in {".jpg", ".jpeg", ".png", ".heic", ".webp"}:
                try:
                    with Image.open(src) as im:
                        res_str = f"{im.width}x{im.height}"
                        if not dt_obj:
                            exif = im._getexif()
                            if exif:
                                for tag_id in (36867, 306, 36868):
                                    if tag_id in exif and exif[tag_id]:
                                        try:
                                            dt_obj = datetime.strptime(str(exif[tag_id])[:19], "%Y:%m:%d %H:%M:%S")
                                            break
                                        except Exception:
                                            pass
                except Exception:
                    pass

            if not dt_obj:
                dt_obj = datetime.fromtimestamp(src.stat().st_mtime)

            date_str = dt_obj.strftime("%Y%m%d%H%M%S")
            device_str = meta.get("cameraModel", "UnknownDevice").replace(" ", "_")
            tag_str = f"{device_str}_{res_str}".strip("_") if res_str else device_str
            new_name = f"{date_str}_[GP_{tag_str}]_{f}"
            target_path = DEST_DIR / new_name

            dup_idx = 1
            while target_path.exists():
                if target_path.stat().st_size == file_size:
                    print(f"  [SKIP] Already exists with same size: {target_path.name}")
                    skipped += 1
                    target_path = None
                    break
                target_path = DEST_DIR / f"{date_str}_[GP_{tag_str}]_{f[:-len(ext)]}_{dup_idx}{ext}"
                dup_idx += 1

            if target_path is None:
                continue

            shutil.move(str(src), str(target_path))
            ts = dt_obj.timestamp()
            os.utime(target_path, (ts, ts))

            integrated += 1
            journal.append({
                "year": 2024,
                "original_name": f,
                "integrated_name": target_path.name,
                "file_size": file_size,
                "capture_time": dt_obj.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "INTEGRATED"
            })
            print(f"  [OK] 2024/{target_path.name}")

    if journal:
        REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        keys = ["year", "original_name", "integrated_name", "file_size", "capture_time", "status"]
        write_header = not REPORT_FILE.exists()
        with open(REPORT_FILE, "a", newline="", encoding="utf-8-sig") as jf:
            writer = csv.DictWriter(jf, fieldnames=keys)
            if write_header:
                writer.writeheader()
            writer.writerows(journal)

    # Clean up empty extracted directory
    try:
        shutil.rmtree(extract_dir)
    except Exception:
        pass

    print(f"\n[완료] 2024년 {integrated}장 편입 완료 (스킵 {skipped}장)")
    return integrated

async def main():
    target_zip = STAGING_DIR / "GooglePhotos_2024.zip"
    ok = await download_2024(target_zip)
    if ok:
        integrate_2024(target_zip)

if __name__ == "__main__":
    asyncio.run(main())
