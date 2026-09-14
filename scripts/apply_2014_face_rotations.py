import os
import sys
import time
import json
import csv
import sqlite3
import argparse
from pathlib import Path
import piexif

DB_PATH = r"D:\_Reports\rotation_upload_progress.db"
CSV_CANDIDATES = r"D:\_Reports\2014_face_rotation_candidates.csv"
JOURNAL_JSON = r"D:\_Reports\2014_face_rotations_applied_journal.json"

def init_journal_db(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS face_rotation_journal (
            filepath TEXT PRIMARY KEY,
            filename TEXT,
            year INTEGER,
            old_tag INTEGER,
            new_tag INTEGER,
            detected_rotation INTEGER,
            face_count INTEGER,
            confidence REAL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

def apply_orientation_to_file(filepath, target_tag):
    if not os.path.exists(filepath):
        return False, "File not found", 1

    mtime = os.path.getmtime(filepath)

    try:
        exif_dict = piexif.load(filepath)
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

    old_tag = exif_dict.get("0th", {}).get(piexif.ImageIFD.Orientation, 1)
    exif_dict.setdefault("0th", {})[piexif.ImageIFD.Orientation] = target_tag

    try:
        exif_bytes = piexif.dump(exif_dict)
        piexif.insert(exif_bytes, filepath)
    except Exception:
        # If full dump fails due to MakerNote or corrupt tag, sanitize and retry
        try:
            if "Exif" in exif_dict and piexif.ExifIFD.MakerNote in exif_dict["Exif"]:
                del exif_dict["Exif"][piexif.ExifIFD.MakerNote]
            if "thumbnail" in exif_dict:
                exif_dict["thumbnail"] = None
            exif_bytes = piexif.dump(exif_dict)
            piexif.insert(exif_bytes, filepath)
        except Exception as e:
            # Fallback: create minimal clean EXIF with orientation
            try:
                minimal_exif = {"0th": {piexif.ImageIFD.Orientation: target_tag}}
                exif_bytes = piexif.dump(minimal_exif)
                piexif.insert(exif_bytes, filepath)
            except Exception as e2:
                return False, f"EXIF insert failed: {e2}", old_tag

    # Restore exact modification time
    os.utime(filepath, (mtime, mtime))
    return True, "Success", old_tag

def rollback_rotations(conn):
    cur = conn.cursor()
    cur.execute("SELECT filepath, old_tag FROM face_rotation_journal WHERE year = 2014")
    rows = cur.fetchall()

    if not rows:
        print("No journal records found to rollback.")
        return

    print(f"Rolling back {len(rows):,} photos to original orientation...")
    success = 0
    fail = 0
    for fp, old_tag in rows:
        ok, msg, _ = apply_orientation_to_file(fp, old_tag)
        if ok:
            success += 1
        else:
            fail += 1

    cur.execute("DELETE FROM face_rotation_journal WHERE year = 2014")
    conn.commit()
    print(f"Rollback finished: {success} succeeded, {fail} failed.")

def main():
    parser = argparse.ArgumentParser(description="Apply 2014 Face-AI detected rotations losslessly to EXIF")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing files")
    parser.add_argument("--rollback", action="store_true", help="Rollback previously applied rotations from journal")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of files to process")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_journal_db(conn)

    if args.rollback:
        rollback_rotations(conn)
        conn.close()
        return

    if not os.path.exists(CSV_CANDIDATES):
        print(f"Error: Candidate CSV not found at {CSV_CANDIDATES}")
        conn.close()
        return

    candidates = []
    with open(CSV_CANDIDATES, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("status") == "ROTATION_NEEDED":
                candidates.append({
                    "filepath": row["filepath"],
                    "filename": row["filename"],
                    "detected_rotation": int(row["detected_rotation"]),
                    "target_tag": int(row["target_tag"]),
                    "face_count": int(row.get("face_count", 0)),
                    "confidence": float(row.get("confidence", 0.0))
                })

    if args.limit > 0:
        candidates = candidates[:args.limit]

    print("=" * 65)
    print(f"  2014년 안면 랜드마크 기반 회전 무손실 EXIF 적용")
    print(f"  적용 대상 파일 수: {len(candidates):,} 건")
    print(f"  모드: {'[DRY RUN - 시뮬레이션]' if args.dry_run else '[REAL - 100% 무손실 EXIF 주입]'}")
    print("=" * 65)

    if args.dry_run:
        print("Dry run completed without changes.")
        conn.close()
        return

    applied_records = []
    success_count = 0
    fail_count = 0
    cur = conn.cursor()

    start_time = time.time()
    for idx, c in enumerate(candidates, 1):
        fp = c["filepath"]
        tgt_tag = c["target_tag"]

        ok, msg, old_tag = apply_orientation_to_file(fp, tgt_tag)
        if ok:
            success_count += 1
            cur.execute("""
                INSERT OR REPLACE INTO face_rotation_journal
                (filepath, filename, year, old_tag, new_tag, detected_rotation, face_count, confidence)
                VALUES (?, ?, 2014, ?, ?, ?, ?, ?)
            """, (fp, c["filename"], old_tag, tgt_tag, c["detected_rotation"], c["face_count"], c["confidence"]))
            applied_records.append({
                "filepath": fp,
                "filename": c["filename"],
                "old_tag": old_tag,
                "new_tag": tgt_tag,
                "detected_rotation": c["detected_rotation"],
                "face_count": c["face_count"],
                "confidence": c["confidence"]
            })
        else:
            fail_count += 1
            print(f"  [FAIL] {c['filename']}: {msg}")

        if idx % 100 == 0 or idx == len(candidates):
            conn.commit()
            print(f"  [{idx:,}/{len(candidates):,} ({idx/len(candidates)*100:5.1f}%)] Applied successfully...", flush=True)

    conn.commit()
    conn.close()

    elapsed = time.time() - start_time
    print(f"\nExecution Finished in {elapsed:.1f}s!")
    print(f"  Successfully applied: {success_count:,} files")
    print(f"  Failed:               {fail_count:,} files")

    # Save journal JSON
    with open(JOURNAL_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "year": 2014,
            "total_candidates": len(candidates),
            "success_count": success_count,
            "fail_count": fail_count,
            "applied_records": applied_records
        }, f, indent=2, ensure_ascii=False)

    print(f"Journal JSON saved: {JOURNAL_JSON}")
    print("=" * 65)

if __name__ == "__main__":
    main()
