import os
import sys
import time
import sqlite3
import csv
import threading
import gc
import ctypes
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor, as_completed
import cv2
import numpy as np
from PIL import Image

# 1. Lower Process Priority to BELOW_NORMAL to prevent PC UI lag/freezing
try:
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetPriorityClass.restype = wintypes.BOOL
    h_proc = kernel32.GetCurrentProcess()
    # 0x00004000 = BELOW_NORMAL_PRIORITY_CLASS
    kernel32.SetPriorityClass(h_proc, 0x00004000)
except Exception as e:
    pass

# 2. Silence OpenCV / libjpeg warnings
try:
    cv2.setLogLevel(0)
except Exception:
    pass

LIBRARY_DIR = r"D:\Photos_Merged"
DB_PATH = r"D:\_Reports\rotation_3stage_audit.db"
CANDIDATES_CSV = r"D:\_Reports\rotation_3stage_candidates.csv"
PROGRESS_LOG = r"D:\_Reports\rotation_3stage_progress.log"

CASCADE_FRONTAL = r"D:\photos_uploader\haarcascades\haarcascade_frontalface_alt2.xml"
CASCADE_DEFAULT = r"D:\photos_uploader\haarcascades\haarcascade_frontalface_default.xml"
CASCADE_PROFILE = r"D:\photos_uploader\haarcascades\haarcascade_profileface.xml"
CASCADE_BODY = r"D:\photos_uploader\haarcascades\haarcascade_upperbody.xml"

thread_local = threading.local()

def get_detectors():
    if not hasattr(thread_local, "frontal_alt2"):
        thread_local.frontal_alt2 = cv2.CascadeClassifier(CASCADE_FRONTAL)
        thread_local.frontal_def = cv2.CascadeClassifier(CASCADE_DEFAULT)
        thread_local.profile = cv2.CascadeClassifier(CASCADE_PROFILE)
        thread_local.body = cv2.CascadeClassifier(CASCADE_BODY)
        thread_local.lsd = cv2.createLineSegmentDetector()
    return (thread_local.frontal_alt2, thread_local.frontal_def, 
            thread_local.profile, thread_local.body, thread_local.lsd)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_results (
            filepath TEXT PRIMARY KEY,
            year TEXT,
            filename TEXT,
            stage TEXT,
            visual_rotation INTEGER,
            current_ori INTEGER,
            target_ori INTEGER,
            match INTEGER,
            confidence REAL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_year ON audit_results(year)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_match ON audit_results(match)")
    conn.commit()
    conn.close()

def get_processed_files():
    if not os.path.exists(DB_PATH):
        return set()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT filepath FROM audit_results")
    processed = {row[0] for row in cur.fetchall()}
    conn.close()
    return processed

def get_existing_candidates():
    existing = set()
    if os.path.exists(CANDIDATES_CSV):
        with open(CANDIDATES_CSV, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            for row in reader:
                if row:
                    existing.add(row[0])
    return existing

def resize_for_eval(img, max_dim=380):
    h, w = img.shape[:2]
    m = max(h, w)
    if m > max_dim:
        s = max_dim / float(m)
        return cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    return img

def rotate_image(img, deg):
    if deg == 0: return img
    elif deg == 90: return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    elif deg == 180: return cv2.rotate(img, cv2.ROTATE_180)
    elif deg == 270: return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img

def evaluate_stage1_person(img_small, detectors):
    f_alt2, f_def, prof, body, _ = detectors
    gray0 = cv2.cvtColor(img_small, cv2.COLOR_BGR2GRAY)
    
    f0 = len(f_alt2.detectMultiScale(gray0, 1.15, 4, minSize=(26, 26)))
    p0 = len(prof.detectMultiScale(gray0, 1.15, 4, minSize=(26, 26)))
    if f0 >= 2:
        return 0, f0 * 5.0
    if f0 == 1 and p0 == 0:
        f_def0 = len(f_def.detectMultiScale(gray0, 1.15, 4, minSize=(26, 26)))
        if f_def0 >= 1:
            return 0, 5.0

    scores = {0: f0 * 4.0 + p0 * 2.0}
    for deg in [90, 180, 270]:
        r = rotate_image(img_small, deg)
        g = cv2.cvtColor(r, cv2.COLOR_BGR2GRAY)
        f_a = len(f_alt2.detectMultiScale(g, 1.15, 4, minSize=(26, 26)))
        f_d = len(f_def.detectMultiScale(g, 1.15, 4, minSize=(26, 26)))
        pl = len(prof.detectMultiScale(g, 1.15, 4, minSize=(26, 26)))
        pr = len(prof.detectMultiScale(cv2.flip(g, 1), 1.15, 4, minSize=(26, 26)))
        score = (f_a + f_d) * 2.5 + (pl + pr) * 2.0
        if deg == 180:
            score *= 0.8
        scores[deg] = score
        
    best_deg = max(scores, key=scores.get)
    best_score = scores[best_deg]
    sorted_s = sorted(scores.values(), reverse=True)
    
    if best_score >= 4.0 and (best_score > sorted_s[1] * 1.4 or sorted_s[1] == 0):
        return best_deg, best_score
    return None, 0

def evaluate_stage2_sky(img_small):
    scores = {}
    blue_scores = {}
    for deg in [0, 90, 180, 270]:
        r = rotate_image(img_small, deg)
        h, w = r.shape[:2]
        top = r[0:int(h * 0.32), :]
        bot = r[h - int(h * 0.32):h, :]
        
        hsv_t = cv2.cvtColor(top, cv2.COLOR_BGR2HSV)
        hsv_b = cv2.cvtColor(bot, cv2.COLOR_BGR2HSV)
        
        blue_t = np.count_nonzero(cv2.inRange(hsv_t, np.array([95, 30, 80]), np.array([130, 255, 255]))) / float(top.shape[0]*top.shape[1])
        blue_b = np.count_nonzero(cv2.inRange(hsv_b, np.array([95, 30, 80]), np.array([130, 255, 255]))) / float(bot.shape[0]*bot.shape[1])
        blue_scores[deg] = blue_t - blue_b * 2.0
        
        white_t = np.count_nonzero(cv2.inRange(hsv_t, np.array([0, 0, 190]), np.array([180, 35, 255]))) / float(top.shape[0]*top.shape[1])
        white_b = np.count_nonzero(cv2.inRange(hsv_b, np.array([0, 0, 190]), np.array([180, 35, 255]))) / float(bot.shape[0]*bot.shape[1])
        
        gray_t = cv2.cvtColor(top, cv2.COLOR_BGR2GRAY)
        gray_b = cv2.cvtColor(bot, cv2.COLOR_BGR2GRAY)
        lap_t = float(cv2.Laplacian(gray_t, cv2.CV_32F).var())
        lap_b = float(cv2.Laplacian(gray_b, cv2.CV_32F).var())
        smooth_bonus = 1.0 if (lap_b > lap_t * 1.5 and lap_t < 150.0) else 0.0
        
        v_diff = (float(np.mean(hsv_t[:, :, 2])) - float(np.mean(hsv_b[:, :, 2]))) / 255.0
        score = blue_scores[deg] * 8.0 + (white_t - white_b * 2.0) * 3.0 + v_diff * 2.5 + smooth_bonus * 2.0
        
        if deg == 180 and blue_t < 0.15:
            score = -10.0
            
        scores[deg] = score
        
    best_deg = max(scores, key=scores.get)
    best_score = scores[best_deg]
    sorted_s = sorted(scores.values(), reverse=True)
    
    if best_score > 3.0 and (best_score - sorted_s[1]) > 1.8:
        return best_deg, best_score
    return None, 0

def evaluate_stage3_vp(img_small, lsd):
    scores = {}
    for deg in [0, 90, 270]:
        r = rotate_image(img_small, deg)
        gray = cv2.cvtColor(r, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        lines = lsd.detect(gray)[0]
        if lines is None or len(lines) < 8:
            scores[deg] = 0
            continue
            
        vert_len = 0
        horiz_len = 0
        slanted = []
        for line in lines:
            x1, y1, x2, y2 = line[0]
            l = np.hypot(x2 - x1, y2 - y1)
            if l < 18: continue
            ang = np.abs(np.arctan2(y2 - y1, x2 - x1) * 180.0 / np.pi)
            if ang > 90: ang = 180 - ang
            if ang > 78:
                vert_len += l
            elif ang < 12:
                horiz_len += l
            elif 25 < ang < 65:
                slanted.append((x1, y1, x2, y2, l))
                
        gravity_score = (vert_len - horiz_len * 0.3) / float(h + 1.0)
        
        vp_score = 0
        if len(slanted) >= 2:
            for i in range(min(10, len(slanted))):
                for j in range(i+1, min(10, len(slanted))):
                    l1, l2 = slanted[i], slanted[j]
                    A1, B1 = l1[3] - l1[1], l1[0] - l1[2]
                    C1 = A1 * l1[0] + B1 * l1[1]
                    A2, B2 = l2[3] - l2[1], l2[0] - l2[2]
                    C2 = A2 * l2[0] + B2 * l2[1]
                    det = A1 * B2 - A2 * B1
                    if abs(det) > 1e-4:
                        x_vp = (B2 * C1 - B1 * C2) / det
                        y_vp = (A1 * C2 - A2 * C1) / det
                        if -0.2 * h <= y_vp <= 0.55 * h and 0.1 * w <= x_vp <= 0.9 * w:
                            vp_score += 1
                            
        scores[deg] = gravity_score * 1.5 + vp_score * 0.4
        
    best_deg = max(scores, key=scores.get)
    best_score = scores[best_deg]
    sorted_s = sorted(scores.values(), reverse=True)
    
    if best_score > 5.0 and (best_score - sorted_s[1]) > 2.0:
        return best_deg, best_score
    return None, 0

def audit_file(filepath, year):
    # Gentle yield to prevent CPU thread starvation
    time.sleep(0.003)
    
    detectors = get_detectors()
    try:
        with open(filepath, 'rb') as fp:
            buf = np.frombuffer(fp.read(), dtype=np.uint8)
        img_raw = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img_raw is None: return None
        img_small = resize_for_eval(img_raw, 380)
        del buf, img_raw
    except:
        return None
        
    current_ori = 1
    try:
        with Image.open(filepath) as pil_img:
            ex = pil_img.getexif()
            if ex: current_ori = ex.get(274, 1)
    except:
        pass
        
    # Stage 1: Person
    deg, conf = evaluate_stage1_person(img_small, detectors)
    stage = "STAGE_1_PERSON"
    if deg is None:
        # Stage 2: Sky
        deg, conf = evaluate_stage2_sky(img_small)
        stage = "STAGE_2_SKY"
        if deg is None:
            # Stage 3: Vanishing Point / Gravity
            deg, conf = evaluate_stage3_vp(img_small, detectors[4])
            stage = "STAGE_3_VP"
            if deg is None:
                deg = 0
                conf = 0
                stage = "DEFAULT_UPRIGHT_0"
                
    rot_to_ori = {0: 1, 90: 6, 180: 3, 270: 8}
    target_ori = rot_to_ori.get(deg, 1)
    match = 1 if (current_ori == target_ori) else 0
    
    return (filepath, year, os.path.basename(filepath), stage, deg, current_ori, target_ori, match, round(conf, 2))

def run_audit(target_year=None):
    init_db()
    processed_set = get_processed_files()
    existing_candidates = get_existing_candidates()
    
    years = [target_year] if target_year else sorted(os.listdir(LIBRARY_DIR))
    all_targets = []
    
    for y in years:
        ypath = os.path.join(LIBRARY_DIR, y)
        if not os.path.isdir(ypath): continue
        for root, dirs, files in os.walk(ypath):
            for f in files:
                if f.lower().endswith(('.jpg', '.jpeg')):
                    fp = os.path.join(root, f)
                    if fp not in processed_set:
                        all_targets.append((fp, y))
                        
    total_remaining = len(all_targets)
    total_total = len(processed_set) + total_remaining
    
    print(f"=== 3-STAGE ROTATION AUDIT RESUMED (LOW-RESOURCE MODE) ===", flush=True)
    print(f"Priority: BELOW_NORMAL_PRIORITY_CLASS (Zero PC Stutter)", flush=True)
    print(f"Workers: 3 Threads with Micro-Pacing", flush=True)
    print(f"Already Audited in DB: {len(processed_set):,} / {total_total:,} ({len(processed_set)/max(1, total_total)*100:.1f}%)", flush=True)
    print(f"Existing Mismatches Recorded: {len(existing_candidates):,}", flush=True)
    print(f"Remaining Targets to Audit: {total_remaining:,}", flush=True)
    
    if total_remaining == 0:
        print("All files in library are already audited!", flush=True)
        return
        
    csv_exists = os.path.exists(CANDIDATES_CSV)
    f_csv = open(CANDIDATES_CSV, "a", newline="", encoding="utf-8-sig")
    csv_writer = csv.writer(f_csv)
    if not csv_exists:
        csv_writer.writerow(["filepath", "year", "filename", "stage", "visual_rotation", "current_ori", "target_ori", "confidence"])
        
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    batch = []
    new_mismatches = 0
    start_time = time.time()
    done_in_run = 0
    
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(audit_file, fp, y): (fp, y) for fp, y in all_targets}
        
        for future in as_completed(futures):
            res = future.result()
            done_in_run += 1
            if res:
                batch.append(res)
                if res[7] == 0: # Mismatch!
                    new_mismatches += 1
                    if res[0] not in existing_candidates:
                        csv_writer.writerow([res[0], res[1], res[2], res[3], res[4], res[5], res[6], res[8]])
                        f_csv.flush()
                        existing_candidates.add(res[0])
                        
            if len(batch) >= 100:
                cur.executemany("""
                    INSERT OR REPLACE INTO audit_results 
                    (filepath, year, filename, stage, visual_rotation, current_ori, target_ori, match, confidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, batch)
                conn.commit()
                batch = []
                
            if done_in_run % 200 == 0 or done_in_run == total_remaining:
                gc.collect()
                now = time.time()
                elapsed = now - start_time
                speed = done_in_run / max(elapsed, 0.001)
                total_done = len(processed_set) + done_in_run
                rem_time = (total_remaining - done_in_run) / max(speed, 0.001)
                log_msg = f"[{total_done:,}/{total_total:,} ({total_done/total_total*100:.1f}%)] RunDone: {done_in_run:,} | NewMismatches: {new_mismatches:,} | TotalMismatches: {len(existing_candidates):,} | Speed: {speed:.1f} f/s | Rem: {rem_time/60:.1f}m"
                print(log_msg, flush=True)
                with open(PROGRESS_LOG, "a", encoding="utf-8") as lf:
                    lf.write(log_msg + "\n")
                    
    if batch:
        cur.executemany("""
            INSERT OR REPLACE INTO audit_results 
            (filepath, year, filename, stage, visual_rotation, current_ori, target_ori, match, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, batch)
        conn.commit()
        
    f_csv.close()
    conn.close()
    
    print(f"=== AUDIT COMPLETE: {done_in_run:,} new files audited. Total Library Mismatches: {len(existing_candidates):,} ===", flush=True)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=str, default=None)
    args = parser.parse_args()
    run_audit(args.year)
