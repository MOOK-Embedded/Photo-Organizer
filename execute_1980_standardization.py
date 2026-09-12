import os
import re
import datetime
import csv
import piexif

JOURNAL_CSV = r"D:\_Reports\canon_g7x_1980_rename_journal.csv"
years = ['2019', '2020', '2021']
targets = []

for y in years:
    yp = os.path.join(r"D:\Photos_Merged", y)
    if not os.path.exists(yp): continue
    for f in sorted(os.listdir(yp)):
        if f.startswith("1980"):
            full = os.path.join(yp, f)
            mtime = os.path.getmtime(full)
            dt_mtime = datetime.datetime.fromtimestamp(mtime)
            targets.append({
                "year": y,
                "old_name": f,
                "old_path": full,
                "mtime": mtime,
                "dt": dt_mtime
            })

print(f"=== STANDARDIZING {len(targets)} CANON G7X 1980 FILES ===")

existing_names_by_year = {}
for y in years:
    yp = os.path.join(r"D:\Photos_Merged", y)
    existing_names_by_year[y] = set(os.listdir(yp)) if os.path.exists(yp) else set()

# Plan clean names
planned = []
for t in targets:
    y = t["year"]
    f = t["old_name"]
    dt = t["dt"]
    ts_str = dt.strftime("%Y%m%d%H%M%S")
    
    m = re.search(r"(\[.*?\])", f)
    if m:
        device_tag = m.group(1)
    else:
        device_tag = "[Canon_PowerShot_G7_X_Mark_II]"
        
    ext = os.path.splitext(f)[1]
    base_candidate = f"{ts_str}_{device_tag}"
    candidate_name = f"{base_candidate}{ext}"
    
    existing_names_by_year[y].remove(f)
    collision_counter = 1
    final_name = candidate_name
    while final_name in existing_names_by_year[y]:
        final_name = f"{base_candidate}_{collision_counter}{ext}"
        collision_counter += 1
        
    existing_names_by_year[y].add(final_name)
    
    t["new_name"] = final_name
    t["new_path"] = os.path.join(os.path.dirname(t["old_path"]), final_name)
    t["new_exif_dt"] = dt.strftime("%Y:%m:%d %H:%M:%S")
    planned.append(t)

# Execute renaming & EXIF DateTime update
f_journal = open(JOURNAL_CSV, "w", newline="", encoding="utf-8-sig")
writer = csv.writer(f_journal)
writer.writerow(["year", "old_path", "new_path", "old_name", "new_name", "exif_date_before", "exif_date_after", "mtime_preserved", "status"])

success_count = 0
for p in planned:
    old_p = p["old_path"]
    new_p = p["new_path"]
    new_dt_str = p["new_exif_dt"]
    mtime_orig = p["mtime"]
    
    try:
        # 1. Load EXIF & update DateTime tags
        exif_dict = piexif.load(old_p)
        dt_before = exif_dict.get("0th", {}).get(piexif.ImageIFD.DateTime, b"1980:01:01").decode("latin1", "ignore")
        
        # Update 0th DateTime (306)
        if "0th" not in exif_dict:
            exif_dict["0th"] = {}
        exif_dict["0th"][piexif.ImageIFD.DateTime] = new_dt_str.encode("latin1")
        
        # Update Exif DateTimeOriginal (36867) and DateTimeDigitized (36868)
        if "Exif" not in exif_dict:
            exif_dict["Exif"] = {}
        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = new_dt_str.encode("latin1")
        exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = new_dt_str.encode("latin1")
        
        # Clean potential non-standard tags
        exif_dict["Exif"].pop(37510, None)
        exif_dict["Exif"].pop(41729, None)
        exif_dict.pop("thumbnail", None)
        
        exif_bytes = piexif.dump(exif_dict)
        piexif.insert(exif_bytes, old_p)
        
        # 2. Rename file
        os.rename(old_p, new_p)
        
        # 3. Restore exact NTFS mtime
        os.utime(new_p, (mtime_orig, mtime_orig))
        
        writer.writerow([p["year"], old_p, new_p, p["old_name"], p["new_name"], dt_before, new_dt_str, "YES", "STANDARDIZED"])
        success_count += 1
        print(f"[{p['year']}] {p['old_name']} -> {p['new_name']}")
        
    except Exception as e:
        print(f"ERROR on {p['old_name']}: {e}")
        writer.writerow([p["year"], old_p, new_p, p["old_name"], p["new_name"], "", new_dt_str, "NO", f"ERROR: {e}"])

f_journal.close()
print(f"\n=== STANDARDIZATION COMPLETE: {success_count}/{len(planned)} files successfully updated! ===")
print(f"Journal saved to: {JOURNAL_CSV}")
