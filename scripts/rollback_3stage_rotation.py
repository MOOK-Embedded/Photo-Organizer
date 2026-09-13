import os
import sys
import time
import csv
import json
import threading
import ctypes
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor, as_completed
import piexif

try:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetPriorityClass.restype = wintypes.BOOL
    h_proc = kernel32.GetCurrentProcess()
    kernel32.SetPriorityClass(h_proc, 0x00004000)
except Exception:
    pass

JOURNAL_CSV = r"D:\_Reports\3stage_rotation_execution_journal.csv"
SUMMARY_JSON = r"D:\_Reports\rollback_3stage_rotation_summary.json"

stats = {
    "total": 0,
    "restored": 0,
    "already_original": 0,
    "missing": 0,
    "error": 0,
}
stats_lock = threading.Lock()

def rollback_single(row):
    filepath = row[0]
    ori_before_str = row[5]
    ori_after_str = row[6]
    
    try:
        ori_before = int(ori_before_str)
        ori_after = int(ori_after_str)
    except:
        return

    if ori_before == ori_after:
        with stats_lock:
            stats["already_original"] += 1
        return

    if not os.path.exists(filepath):
        with stats_lock:
            stats["missing"] += 1
        return

    try:
        mtime = os.path.getmtime(filepath)
        exif = piexif.load(filepath)
        cur_ori = exif.get("0th", {}).get(piexif.ImageIFD.Orientation, 1)
        
        if cur_ori == ori_before:
            with stats_lock:
                stats["already_original"] += 1
            return

        if "0th" not in exif:
            exif["0th"] = {}
        exif["0th"][piexif.ImageIFD.Orientation] = ori_before

        if "Exif" in exif:
            exif["Exif"].pop(37510, None)
            exif["Exif"].pop(41729, None)
        exif.pop("thumbnail", None)

        b = piexif.dump(exif)
        piexif.insert(b, filepath)
        os.utime(filepath, (mtime, mtime))

        with stats_lock:
            stats["restored"] += 1
    except Exception as e:
        with stats_lock:
            stats["error"] += 1

def main():
    if not os.path.exists(JOURNAL_CSV):
        print("Journal not found:", JOURNAL_CSV)
        return

    with open(JOURNAL_CSV, "r", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))[1:]

    stats["total"] = len(rows)
    print(f"Starting 100% EXIF Orientation Rollback for {len(rows):,} files...")
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(rollback_single, r) for r in rows]
        done_count = 0
        for fut in as_completed(futures):
            done_count += 1
            if done_count % 2000 == 0 or done_count == len(rows):
                elapsed = time.time() - start_time
                speed = done_count / elapsed if elapsed > 0 else 0
                print(f"[{done_count:,}/{len(rows):,} ({done_count/len(rows)*100:5.1f}%)] Restored: {stats["restored"]:,} | Already: {stats["already_original"]:,} | Speed: {speed:5.1f} f/s", flush=True)

    elapsed = time.time() - start_time
    stats["elapsed_sec"] = round(elapsed, 1)
    with open(SUMMARY_JSON, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 60)
    print(f"Rollback Complete in {elapsed/60:.2f} minutes!")
    print(f"  Total files:    {stats["total"]:,}")
    print(f"  Restored:       {stats["restored"]:,}")
    print(f"  Already orig:   {stats["already_original"]:,}")
    print(f"  Missing:        {stats["missing"]:,}")
    print(f"  Errors:         {stats["error"]:,}")
    print("=" * 60)

if __name__ == "__main__":
    main()
