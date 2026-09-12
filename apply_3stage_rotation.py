import os
import sys
import time
import csv
import threading
import ctypes
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor, as_completed
import piexif

# 1. Lower Process Priority to BELOW_NORMAL to prevent PC UI lag/freezing
try:
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetPriorityClass.restype = wintypes.BOOL
    h_proc = kernel32.GetCurrentProcess()
    # 0x00004000 = BELOW_NORMAL_PRIORITY_CLASS
    kernel32.SetPriorityClass(h_proc, 0x00004000)
except Exception:
    pass

CANDIDATES_CSV = r"D:\_Reports\rotation_3stage_candidates.csv"
JOURNAL_CSV = r"D:\_Reports\3stage_rotation_execution_journal.csv"
PROGRESS_LOG = r"D:\_Reports\3stage_rotation_apply_progress.log"

journal_lock = threading.Lock()
count_success = 0
count_already_correct = 0
count_missing = 0
count_error = 0

def apply_lossless_orientation(row):
    global count_success, count_already_correct, count_missing, count_error
    filepath, year, filename, stage, visual_rot, cur_ori_str, target_ori_str, conf = row[:8]
    target_ori = int(target_ori_str)
    
    if not os.path.exists(filepath):
        with journal_lock:
            count_missing += 1
        return [filepath, year, filename, stage, visual_rot, cur_ori_str, target_ori, "NO", "FILE_NOT_FOUND"]
        
    try:
        # 1. Capture exact mtime
        mtime_orig = os.path.getmtime(filepath)
        
        # 2. Load EXIF
        exif_dict = piexif.load(filepath)
        ori_before = exif_dict.get("0th", {}).get(piexif.ImageIFD.Orientation, 1)
        
        if ori_before == target_ori:
            with journal_lock:
                count_already_correct += 1
            return [filepath, year, filename, stage, visual_rot, ori_before, target_ori, "YES", "ALREADY_CORRECT"]
            
        # 3. Modify Tag 274 in 0th IFD
        if "0th" not in exif_dict:
            exif_dict["0th"] = {}
        exif_dict["0th"][piexif.ImageIFD.Orientation] = target_ori
        
        # 4. Clean non-standard tags
        if "Exif" in exif_dict:
            exif_dict["Exif"].pop(37510, None) # Samsung Note 3 UserComment overflow
            exif_dict["Exif"].pop(41729, None) # SceneType
        exif_dict.pop("thumbnail", None)
        
        # 5. Zero-loss inject into APP1
        exif_bytes = piexif.dump(exif_dict)
        piexif.insert(exif_bytes, filepath)
        
        # 6. Restore exact original mtime
        os.utime(filepath, (mtime_orig, mtime_orig))
        
        with journal_lock:
            count_success += 1
        return [filepath, year, filename, stage, visual_rot, ori_before, target_ori, "YES", "APPLIED_LOSSLESS"]
        
    except Exception as e:
        with journal_lock:
            count_error += 1
        return [filepath, year, filename, stage, visual_rot, cur_ori_str, target_ori, "NO", f"ERROR: {e}"]

def run_apply():
    if not os.path.exists(CANDIDATES_CSV):
        print(f"ERROR: {CANDIDATES_CSV} does not exist!", flush=True)
        return
        
    targets = []
    with open(CANDIDATES_CSV, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        for r in reader:
            if r and len(r) >= 7:
                targets.append(r)
                
    total = len(targets)
    print(f"=== 100% LOSSLESS EXIF ORIENTATION INJECTION ===", flush=True)
    print(f"Priority: BELOW_NORMAL_PRIORITY_CLASS (Zero PC Stutter)", flush=True)
    print(f"Total Targets: {total:,} files", flush=True)
    print(f"Engine: piexif binary APP1 insertion (Zero JPEG re-encoding, Zero generation loss)", flush=True)
    print(f"Timestamp: Exact os.utime restoration", flush=True)
    print(f"Journal: {JOURNAL_CSV}", flush=True)
    print("-" * 60, flush=True)
    
    f_journal = open(JOURNAL_CSV, "w", newline="", encoding="utf-8-sig")
    writer = csv.writer(f_journal)
    writer.writerow(["filepath", "year", "filename", "stage", "visual_rotation", "orientation_before", "orientation_after", "mtime_preserved", "status"])
    
    start_time = time.time()
    processed = 0
    
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(apply_lossless_orientation, row): row for row in targets}
        for future in as_completed(futures):
            res = future.result()
            with journal_lock:
                writer.writerow(res)
                processed += 1
                curr = processed
                
            if curr % 500 == 0 or curr == total:
                f_journal.flush()
                now = time.time()
                elapsed = now - start_time
                speed = curr / max(elapsed, 0.001)
                rem = (total - curr) / max(speed, 0.001)
                log_msg = f"[{curr:,}/{total:,} ({curr/total*100:.1f}%)] Applied: {count_success:,} | Speed: {speed:.1f} f/s | Rem: {rem/60:.1f}m"
                print(log_msg, flush=True)
                with open(PROGRESS_LOG, "a", encoding="utf-8") as lf:
                    lf.write(log_msg + "\n")
                    
    f_journal.close()
    print(f"=== INJECTION COMPLETE ===", flush=True)
    print(f"Successfully Applied: {count_success:,}", flush=True)
    print(f"Already Correct: {count_already_correct:,}", flush=True)
    print(f"Errors: {count_error:,}", flush=True)
    print(f"Missing: {count_missing:,}", flush=True)

if __name__ == "__main__":
    run_apply()
