import os
import sys
import time
import json
import csv
import sqlite3
import concurrent.futures
from pathlib import Path
import cv2
import numpy as np
from PIL import Image

MODEL_PATH = r"D:\photos_uploader\models\face_detection_yunet_2023mar.onnx"
DB_PATH = r"D:\_Reports\rotation_upload_progress.db"
CSV_OUTPUT = r"D:\_Reports\2014_face_rotation_candidates.csv"
SUMMARY_OUTPUT = r"D:\_Reports\2014_face_rotation_summary.json"
GALLERY_HTML = r"D:\photos_uploader\reports\2014_rotation_candidates_gallery.html"
THUMB_DIR = Path(r"D:\photos_uploader\reports\rotation_thumbs_2014")

ROTATION_TO_TAG = {
    0: 1,
    90: 6,
    180: 3,
    270: 8
}

def analyze_single_photo(filepath):
    if not os.path.exists(filepath):
        return None

    # Fast read with OpenCV
    try:
        # On Windows, cv2.imdecode handles Korean/UTF-8 paths cleanly
        data = np.fromfile(filepath, dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if img is None:
            return None
    except Exception:
        return None

    h_orig, w_orig = img.shape[:2]

    # Test 4 rotations: 0, 90, 180, 270 CW
    rotations = [
        (0, None),
        (90, cv2.ROTATE_90_CLOCKWISE),
        (180, cv2.ROTATE_180),
        (270, cv2.ROTATE_90_COUNTERCLOCKWISE)
    ]

    scores = {}
    best_faces_info = {}

    for deg, code in rotations:
        if code is None:
            rotated = img
        else:
            rotated = cv2.rotate(img, code)

        rh, rw = rotated.shape[:2]
        # Max dimension 640 for fast, accurate YuNet inference
        scale = min(1.0, 640.0 / max(rh, rw))
        inp_w = int(rw * scale)
        inp_h = int(rh * scale)
        small = cv2.resize(rotated, (inp_w, inp_h))

        detector = cv2.FaceDetectorYN.create(
            model=MODEL_PATH,
            config="",
            input_size=(inp_w, inp_h),
            score_threshold=0.6,
            nms_threshold=0.3,
            top_k=10
        )

        _, faces = detector.detect(small)

        valid_faces = []
        if faces is not None:
            for f in faces:
                conf = float(f[-1])
                re = (f[4], f[5])
                le = (f[6], f[7])
                eye_center_y = (re[1] + le[1]) / 2.0
                mouth_center_y = (f[11] + f[13]) / 2.0

                # Upright check: eyes above mouth
                is_upright = eye_center_y < mouth_center_y
                # Tilt check: eyes relatively horizontal
                eye_dx = abs(re[0] - le[0])
                eye_dy = abs(re[1] - le[1])
                angle_deg = np.degrees(np.arctan2(eye_dy, eye_dx + 1e-6))

                if is_upright and angle_deg < 42.0:
                    valid_faces.append({
                        "conf": conf,
                        "tilt": float(angle_deg)
                    })

        total_conf = sum(vf["conf"] for vf in valid_faces)
        scores[deg] = total_conf
        best_faces_info[deg] = len(valid_faces)

    # Determine best orientation
    # 1. If no faces at any angle: None (keep original, do not touch!)
    max_score = max(scores.values())
    if max_score < 0.65:
        return {
            "filepath": filepath,
            "filename": os.path.basename(filepath),
            "status": "NO_FACES_PRESERVED",
            "detected_rotation": 0,
            "target_tag": 1,
            "face_count": 0,
            "confidence": 0.0
        }

    # 2. Angle with highest score
    best_deg = max(scores.keys(), key=lambda k: (scores[k], best_faces_info[k]))

    # Safe margin: If 0 deg has faces and is close to best_deg, prefer 0 deg (do not rotate unnecessarily)
    if best_deg != 0 and scores[0] >= 0.8 * scores[best_deg] and best_faces_info[0] >= best_faces_info[best_deg]:
        best_deg = 0

    status = "UPRIGHT_NORMAL" if best_deg == 0 else "ROTATION_NEEDED"

    return {
        "filepath": filepath,
        "filename": os.path.basename(filepath),
        "status": status,
        "detected_rotation": best_deg,
        "target_tag": ROTATION_TO_TAG[best_deg],
        "face_count": best_faces_info[best_deg],
        "confidence": round(scores[best_deg], 2),
        "scores": scores
    }

def generate_comparison_thumb(filepath, deg, out_dir):
    try:
        im = Image.open(filepath)
        w, h = im.size

        # Left: Original thumbnail
        thumb_orig = im.copy()
        thumb_orig.thumbnail((300, 300))

        # Right: Rotated thumbnail
        # PIL rotate is CCW, so deg CW is 360 - deg
        thumb_rot = im.copy().rotate(360 - deg, expand=True)
        thumb_rot.thumbnail((300, 300))

        # Canvas for side-by-side
        cw = thumb_orig.width + thumb_rot.width + 20
        ch = max(thumb_orig.height, thumb_rot.height)
        canvas = Image.new("RGB", (cw, ch), (30, 30, 30))
        canvas.paste(thumb_orig, (0, (ch - thumb_orig.height) // 2))
        canvas.paste(thumb_rot, (thumb_orig.width + 20, (ch - thumb_rot.height) // 2))

        base_name = Path(filepath).stem[:50] + "_compare.jpg"
        out_path = out_dir / base_name
        canvas.save(str(out_path), quality=85)
        return str(out_path.name)
    except Exception:
        return ""

def main():
    print("=" * 65)
    print("  2014년 사진 고정밀 딥러닝 안면 랜드마크 회전 감사 (YuNet)  ")
    print("=" * 65)

    THUMB_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Fetch all 2014 photos from DB
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT filepath FROM upload_queue WHERE year = 2014 ORDER BY filepath ASC")
    all_files = [r[0] for r in cur.fetchall()]
    conn.close()

    total_count = len(all_files)
    print(f"Total 2014 photos to audit: {total_count:,} files\n")

    start_time = time.time()
    results = []
    rotation_candidates = []

    # Multi-threaded audit with 12 workers
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        future_to_file = {executor.submit(analyze_single_photo, fp): fp for fp in all_files}
        done = 0
        for fut in concurrent.futures.as_completed(future_to_file):
            done += 1
            res = fut.result()
            if res:
                results.append(res)
                if res["status"] == "ROTATION_NEEDED":
                    rotation_candidates.append(res)

            if done % 200 == 0 or done == total_count:
                elapsed = time.time() - start_time
                fps = done / elapsed if elapsed > 0 else 0
                print(f"[{done:,}/{total_count:,} ({done/total_count*100:5.1f}%)] "
                      f"Rotations needed: {len(rotation_candidates):,} | Speed: {fps:5.1f} f/s", flush=True)

    elapsed_total = time.time() - start_time
    print(f"\nAudit completed in {elapsed_total:.1f}s ({elapsed_total/60:.2f} min)!")
    print(f"  Total analyzed:     {len(results):,}")
    print(f"  Upright normal:     {sum(1 for r in results if r['status'] == 'UPRIGHT_NORMAL'):,}")
    print(f"  No faces/preserved: {sum(1 for r in results if r['status'] == 'NO_FACES_PRESERVED'):,}")
    print(f"  ROTATION NEEDED:    {len(rotation_candidates):,}")

    # Breakdown by rotation degree
    deg_counts = {90: 0, 180: 0, 270: 0}
    for c in rotation_candidates:
        deg_counts[c["detected_rotation"]] = deg_counts.get(c["detected_rotation"], 0) + 1
    print(f"    - 90° CW (Tag 6):  {deg_counts.get(90, 0):,} files")
    print(f"    - 180° (Tag 3):    {deg_counts.get(180, 0):,} files")
    print(f"    - 270° CW (Tag 8): {deg_counts.get(270, 0):,} files")

    # 2. Save CSV
    print(f"\nSaving CSV report to {CSV_OUTPUT}...")
    with open(CSV_OUTPUT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["filepath", "filename", "status", "detected_rotation", "target_tag", "face_count", "confidence"])
        for r in results:
            w.writerow([r["filepath"], r["filename"], r["status"], r["detected_rotation"], r["target_tag"], r["face_count"], r["confidence"]])

    # 3. Generate side-by-side comparison thumbnails for candidates
    print(f"\nGenerating side-by-side comparison thumbnails for {len(rotation_candidates):,} candidates...")
    thumb_start = time.time()
    for idx, c in enumerate(rotation_candidates, 1):
        thumb_name = generate_comparison_thumb(c["filepath"], c["detected_rotation"], THUMB_DIR)
        c["thumb_name"] = thumb_name
        if idx % 50 == 0 or idx == len(rotation_candidates):
            print(f"  [{idx}/{len(rotation_candidates)}] Thumbnails created...", flush=True)

    # 4. Generate Interactive HTML Gallery
    print(f"\nGenerating HTML Visual Gallery: {GALLERY_HTML}...")
    html_cards = []
    for c in rotation_candidates:
        tname = c.get("thumb_name", "")
        img_tag = f'<img src="rotation_thumbs_2014/{tname}" loading="lazy">' if tname else '<p>No Thumb</p>'
        card = f"""
        <div class="card">
            <div class="card-header">
                <span class="deg-badge deg-{c['detected_rotation']}">{c['detected_rotation']}° CW 회전 필요</span>
                <span class="tag-badge">EXIF Tag {c['target_tag']}</span>
                <span class="conf-badge">인원: {c['face_count']}명 (신뢰도: {c['confidence']})</span>
            </div>
            <div class="img-container">
                {img_tag}
            </div>
            <div class="labels">
                <span>◀ 현재 원본 상태 (누움/뒤집힘)</span>
                <span>교정 후 (정방향 똑바로) ▶</span>
            </div>
            <div class="card-footer">
                <div class="filename" title="{c['filepath']}">{c['filename']}</div>
            </div>
        </div>
        """
        html_cards.append(card)

    html_content = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>2014년 사진 회전 교정 후보 육안 검토 갤러리 (YuNet 딥러닝)</title>
<style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #18191a; color: #e4e6eb; margin: 0; padding: 20px; }}
    h1 {{ color: #ffffff; margin-bottom: 8px; }}
    .subtitle {{ color: #b0b3b8; font-size: 15px; margin-bottom: 24px; }}
    .summary-box {{ background: #242526; border-radius: 10px; padding: 16px 20px; margin-bottom: 24px; display: flex; gap: 30px; border: 1px solid #3a3b3c; }}
    .stat-item {{ display: flex; flex-direction: column; }}
    .stat-num {{ font-size: 24px; font-weight: bold; color: #4e9af1; }}
    .stat-label {{ font-size: 12px; color: #b0b3b8; margin-top: 4px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(480px, 1fr)); gap: 20px; }}
    .card {{ background: #242526; border-radius: 12px; border: 1px solid #3a3b3c; overflow: hidden; display: flex; flex-direction: column; }}
    .card-header {{ padding: 12px 16px; background: #2a2b2d; display: flex; align-items: center; gap: 8px; border-bottom: 1px solid #3a3b3c; }}
    .deg-badge {{ padding: 4px 10px; border-radius: 6px; font-weight: bold; font-size: 13px; }}
    .deg-90 {{ background: #f59e0b; color: #000; }}
    .deg-180 {{ background: #ef4444; color: #fff; }}
    .deg-270 {{ background: #3b82f6; color: #fff; }}
    .tag-badge {{ background: #374151; color: #d1d5db; padding: 4px 8px; border-radius: 6px; font-size: 12px; }}
    .conf-badge {{ margin-left: auto; font-size: 12px; color: #9ca3af; }}
    .img-container {{ background: #111; padding: 10px; display: flex; justify-content: center; }}
    .img-container img {{ max-width: 100%; height: auto; border-radius: 6px; }}
    .labels {{ display: flex; justify-content: space-between; padding: 4px 16px; font-size: 11px; color: #9ca3af; font-weight: bold; }}
    .card-footer {{ padding: 10px 16px; font-size: 12px; color: #9ca3af; word-break: break-all; }}
    .filename {{ font-family: monospace; font-size: 12px; color: #e5e7eb; }}
</style>
</head>
<body>
    <h1>📸 2014년 사진 회전 교정 후보 전수 시각 검토 갤러리</h1>
    <div class="subtitle">AI 랜드마크 분석으로 감지된 실제 누워있거나 뒤집힌 사진 {len(rotation_candidates):,}장의 [회전 전 ➔ 회전 후] 나란히 비교</div>
    
    <div class="summary-box">
        <div class="stat-item">
            <span class="stat-num">{total_count:,}</span>
            <span class="stat-label">2014년 전체 사진</span>
        </div>
        <div class="stat-item">
            <span class="stat-num" style="color: #ef4444;">{len(rotation_candidates):,}</span>
            <span class="stat-label">회전 교정 필요 (누움/뒤집힘)</span>
        </div>
        <div class="stat-item">
            <span class="stat-num" style="color: #10b981;">{sum(1 for r in results if r['status'] == 'UPRIGHT_NORMAL'):,}</span>
            <span class="stat-label">이미 정방향 (안면 확인)</span>
        </div>
        <div class="stat-item">
            <span class="stat-num" style="color: #6b7280;">{sum(1 for r in results if r['status'] == 'NO_FACES_PRESERVED'):,}</span>
            <span class="stat-label">풍경/정물 (원본 100% 보존)</span>
        </div>
        <div class="stat-item">
            <span class="stat-num">{deg_counts.get(90, 0)} / {deg_counts.get(180, 0)} / {deg_counts.get(270, 0)}</span>
            <span class="stat-label">90° CW / 180° / 270° CW</span>
        </div>
    </div>

    <div class="grid">
        {"".join(html_cards)}
    </div>
</body>
</html>
"""
    with open(GALLERY_HTML, "w", encoding="utf-8") as f:
        f.write(html_content)

    # 5. Summary JSON
    summary = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "year": 2014,
        "total_files": total_count,
        "rotation_needed_count": len(rotation_candidates),
        "upright_normal_count": sum(1 for r in results if r['status'] == 'UPRIGHT_NORMAL'),
        "no_faces_count": sum(1 for r in results if r['status'] == 'NO_FACES_PRESERVED'),
        "degrees_breakdown": deg_counts,
        "elapsed_seconds": round(elapsed_total, 1),
        "gallery_path": GALLERY_HTML,
        "csv_path": CSV_OUTPUT
    }
    with open(SUMMARY_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"\nSummary JSON saved: {SUMMARY_OUTPUT}")
    print(f"HTML Gallery created: {GALLERY_HTML}")
    print("=" * 65)

if __name__ == "__main__":
    main()
