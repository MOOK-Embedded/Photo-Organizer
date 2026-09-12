import csv
import json
import re
from pathlib import Path
from collections import Counter

csv_path = Path(r"D:\_Reports\google_photos_cloud_all_albums.csv")
json_path = Path(r"D:\_Reports\google_photos_deep_crawl.json")

albums = []
total_items = 0
with open(csv_path, "r", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for r in reader:
        cnt = int(r["Item_Count_Num"]) if r["Item_Count_Num"] else 0
        total_items += cnt
        albums.append({
            "title": r["Album_Title"],
            "count_str": r["Item_Count_Str"],
            "count": cnt,
            "href": r["Href"]
        })

print(f"Total Albums Scraped: {len(albums)}")
print(f"Total Photos/Videos across all albums: {total_items:,}")

# Categorize albums
folder_path_albums = [a for a in albums if "\\" in a["title"] or "/" in a["title"]]
regular_named_albums = [a for a in albums if "\\" not in a["title"] and "/" not in a["title"]]

print(f"\n1. Folder-path auto-created albums (e.g. contain '\\'): {len(folder_path_albums)} albums ({sum(a['count'] for a in folder_path_albums):,} items)")
print(f"2. Regular/Named albums (e.g. '울아들 시영'): {len(regular_named_albums)} albums ({sum(a['count'] for a in regular_named_albums):,} items)")

print("\n--- Top 20 Largest Albums ---")
for a in sorted(albums, key=lambda x: -x["count"])[:20]:
    print(f"  {a['count']:>6,} items | {a['title']}")

print("\n--- Regular / Named Albums List ---")
for a in sorted(regular_named_albums, key=lambda x: -x["count"]):
    print(f"  {a['count']:>6,} items | {a['title']}")

# Group by folder prefix
prefixes = Counter()
for a in folder_path_albums:
    prefix = a["title"].split("\\")[0]
    prefixes[prefix] += 1

print("\n--- Folder Path Album Prefixes ---")
for p, c in prefixes.most_common(15):
    items_in_p = sum(a["count"] for a in folder_path_albums if a["title"].startswith(p))
    print(f"  {p}: {c} albums ({items_in_p:,} items)")
