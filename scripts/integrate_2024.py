import os
import sys
import json
import csv
import zipfile
import shutil
from datetime import datetime
from pathlib import Path
from PIL import Image

def integrate_zip(zip_path: Path, year: int = 2024):
    if not zip_path.exists():
        print(f"[ERROR] {zip_path} does not exist!")
        return 0

    extract_dir = zip_path.parent / "extracted_2024"
    extract_dir.mkdir(parents=True, exist_ok=True)
    dest_dir = Path(r"D:\Photos_Merged") / str(year)
    dest_dir.mkdir(parents=True, exist_ok=True)
    report_file = Path(r"D:\_Reports\chrome_bot_download_journal.csv")

    print(f"\n[1/3] Extracting {zip_path.name} ({zip_path.stat().st_size / (1024*1024*1024):.2f} GB)...")
    with zipfile.ZipFile(zip_path, 'r') as z:
        members = z.namelist()
        total = len(members)
        print(f"  Total entries in ZIP: {total}")
        for i, member in enumerate(members, 1):
            z.extract(member, extract_dir)
            if i % 100 == 0 or i == total:
                print(f"  Extracted {i}/{total}...")
    print(f"  Extraction complete to: {extract_dir}")

    # Index sidecar JSONs
    print("\n[2/3] Indexing Google Photos sidecar JSON metadata...")
    json_meta = {}
    for root, _, files in os.walk(extract_dir):
        for f in files:
            if f.endswith(".json"):
                jp = Path(root) / f
                try:
                    with open(jp, "r", encoding="utf-8") as jf:
                        jdata = json.load(jf)
                        base_name = f[:-5]
                        json_meta[base_name] = jdata
                except Exception:
                    pass
    print(f"  Found {len(json_meta)} metadata JSON files.")

    # Process media files
    print(f"\n[3/3] Standardizing and integrating media into {dest_dir}...")
    valid_exts = {".jpg", ".jpeg", ".png", ".heic", ".mp4", ".mov", ".dng", ".webp"}
    integrated_count = 0
    skipped_count = 0
    journal = []

    for root, _, files in os.walk(extract_dir):
        for f in files:
            ext = Path(f).suffix.lower()
            if ext not in valid_exts:
                continue

            src_file = Path(root) / f
            file_size = src_file.stat().st_size

            # Timestamp extraction: sidecar JSON first
            dt_obj = None
            meta = json_meta.get(f, {})
            taken_time = meta.get("photoTakenTime", {}).get("timestamp")
            if taken_time:
                try:
                    dt_obj = datetime.fromtimestamp(int(taken_time))
                except Exception:
                    pass

            res_str = ""
            if ext in {".jpg", ".jpeg", ".png", ".heic", ".webp"}:
                try:
                    with Image.open(src_file) as im:
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

            # Fallback: file mtime
            if not dt_obj:
                dt_obj = datetime.fromtimestamp(src_file.stat().st_mtime)

            date_str = dt_obj.strftime("%Y%m%d%H%M%S")
            device_str = meta.get("cameraModel", "UnknownDevice").replace(" ", "_")
            tag_str = f"{device_str}_{res_str}".strip("_") if res_str else device_str
            new_name = f"{date_str}_[GP_{tag_str}]_{f}"
            target_path = dest_dir / new_name

            # Avoid filename collisions
            dup_idx = 1
            while target_path.exists():
                if target_path.stat().st_size == file_size:
                    print(f"  [SKIP] Already exists (same size): {target_path.name}")
                    skipped_count += 1
                    target_path = None
                    break
                target_path = dest_dir / f"{date_str}_[GP_{tag_str}]_{f[:-len(ext)]}_{dup_idx}{ext}"
                dup_idx += 1

            if target_path is None:
                continue

            # Move file
            shutil.move(str(src_file), str(target_path))

            # Restore exact mtime
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
            if integrated_count % 20 == 0 or integrated_count <= 5:
                print(f"  [{integrated_count}] {year}/{target_path.name}")

    print(f"\n  Total integrated so far: {integrated_count}")

    if journal:
        report_file.parent.mkdir(parents=True, exist_ok=True)
        keys = ["year", "original_name", "integrated_name", "file_size", "capture_time", "status"]
        write_header = not report_file.exists()
        with open(report_file, "a", newline="", encoding="utf-8-sig") as jf:
            writer = csv.DictWriter(jf, fieldnames=keys)
            if write_header:
                writer.writeheader()
            writer.writerows(journal)
        print(f"  Journal updated: {report_file}")

    # Cleanup extract dir
    print(f"\n[Cleanup] Removing extraction directory: {extract_dir}")
    shutil.rmtree(extract_dir, ignore_errors=True)

    print("\n" + "=" * 60)
    print(f"🎉 2024 Integration Complete!")
    print(f"  - Integrated : {integrated_count:,} files")
    print(f"  - Skipped    : {skipped_count:,} files (identical duplicates)")
    print(f"  - Destination: {dest_dir}")
    print("=" * 60)
    return integrated_count

if __name__ == "__main__":
    zip_p = Path(r"D:\_GooglePhotos_Recent_Downloads\2024\GooglePhotos_2024.zip")
    integrate_zip(zip_p, 2024)
