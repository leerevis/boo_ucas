#!/usr/bin/env python3
"""
Build data.json for the IR/Politics course comparison site.

Inputs (in this same folder — flat layout, no ../output/):
  - ir_courses_final_target_list.csv   master list of 267 target courses
  - modules_flat_bucketed.csv          cleaned, bucketed module rows (preferred)
      falls back to modules_flat.csv if the bucketed file isn't present yet
      (bucket_1/bucket_2 columns are then treated as empty — every module
      shows up unbucketed until you run the bucket-review pass)
  - atlas_data.json                    OPTIONAL: institution -> region lookup,
      borrowed from the earlier IR Course Atlas project, purely for the Map
      tab. Not present right now, so every institution falls back to
      "Unknown region" until you track that file down and drop it in here.
      (Deliberately NOT named data.json — that name is this script's own
      output, so reusing it would make the script overwrite its own input.)

Output:
  - data.json  (in this folder, alongside index.html)

Re-run this after any future scrape + re-clean + re-bucket cycle to refresh the site's data.
"""
import csv
import json
import os
import re
import sys
from collections import OrderedDict, defaultdict

BUCKETS = [
    "Security & Conflict", "IR Theory & Global Order", "International Political Economy",
    "Comparative & British Politics", "Political Theory & Philosophy", "Human Rights & Humanitarianism",
    "Regional & Area Studies", "Diplomacy & Foreign Policy", "Law", "Gender, Identity & Society",
    "Environment & Sustainability", "Media, Communication & Culture", "Research Methods & Dissertation",
    "Language & Area Language Modules", "Skills, Placement & Employability", "Sociology & History",
]

YEAR_ORDER = ["Year 1", "Year 2", "Year 3", "Year 4", "Year 5"]

def year_sort_key(y):
    m = re.search(r"\d+", y or "")
    return int(m.group()) if m else 99

def slugify(inst, course):
    s = f"{inst}-{course}".lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s

def load_region_by_institution():
    """Region per institution, borrowed from the earlier IR Course Atlas
    dataset (atlas_data.json) purely for the Map view -- we don't pull
    in any of its ranking/fee/entry-requirement fields, just geography.
    Optional: if the file isn't here, every institution is "Unknown"."""
    try:
        with open("atlas_data.json", encoding="utf-8") as f:
            atlas = json.load(f)
        return {d["inst"]: d.get("region") for d in atlas if d.get("region")}
    except (FileNotFoundError, json.JSONDecodeError):
        print("note: atlas_data.json not found -- Map tab will show every institution as 'Unknown region'")
        return {}

def load_modules():
    """Prefer the bucketed file; fall back to the pre-bucket flat file with
    a warning. Either way, missing bucket_1/bucket_2 columns are treated as
    blank rather than raising, so the site still builds -- just without
    bucket tagging until the review pass is done."""
    if os.path.exists("modules_flat_bucketed.csv"):
        with open("modules_flat_bucketed.csv", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    if os.path.exists("modules_flat.csv"):
        print("note: modules_flat_bucketed.csv not found -- using modules_flat.csv instead.")
        print("      This file has no bucket_1/bucket_2 columns, so every module will show up")
        print("      unbucketed (no Security & Conflict / IR Theory / etc. tags) until you run")
        print("      the bucket-review pass and save the result as modules_flat_bucketed.csv.")
        with open("modules_flat.csv", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    sys.exit("error: neither modules_flat_bucketed.csv nor modules_flat.csv found in this folder")

def main():
    with open("ir_courses_final_target_list.csv", newline="", encoding="utf-8") as f:
        target = list(csv.DictReader(f))
    mods = load_modules()
    region_by_inst = load_region_by_institution()

    mods_by_course = defaultdict(list)
    for r in mods:
        key = (r["institution"].strip(), r["course_name"].strip())
        mods_by_course[key].append(r)

    courses = []
    for r in target:
        inst = r["institution"].strip()
        course_name = r["course_name"].strip()
        url = r["url"].strip()
        key = (inst, course_name)
        rows = mods_by_course.get(key, [])

        years = defaultdict(list)
        bucket_counts = defaultdict(int)
        for m in rows:
            y = (m["year"] or "").strip() or "Unspecified"
            bucket_list = [b for b in (m.get("bucket_1", ""), m.get("bucket_2", "")) if b and b != "Uncategorized"]
            for b in bucket_list:
                bucket_counts[b] += 1
            years[y].append({
                "name": m["module_name"].strip(),
                "credits": m["credits"].strip(),
                "type": m["type"].strip(),
                "buckets": bucket_list,
            })

        year_keys = sorted(years.keys(), key=year_sort_key)
        years_ordered = OrderedDict((y, years[y]) for y in year_keys)

        courses.append({
            "id": slugify(inst, course_name),
            "institution": inst,
            "course_name": course_name,
            "url": url,
            "region": region_by_inst.get(inst) or "Unknown",
            "badges": (r.get("badges") or "").strip(),
            "partner_subjects": (r.get("partner_subjects") or "").strip(),
            "has_modules": len(rows) > 0,
            "module_count": len(rows),
            "years": years_ordered,
            "bucket_counts": dict(bucket_counts),
        })

    courses.sort(key=lambda c: (c["institution"], c["course_name"]))

    institutions = sorted(set(c["institution"] for c in courses))

    data = {
        "generated_note": "Static snapshot -- rebuild by re-running scrape_modules.py, the cleaning pass, and this script.",
        "buckets": BUCKETS,
        "institutions": institutions,
        "courses": courses,
        "stats": {
            "total_courses": len(courses),
            "courses_with_modules": sum(1 for c in courses if c["has_modules"]),
            "total_module_rows": sum(c["module_count"] for c in courses),
        },
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    # Bake the data straight into index.html (template.html + data -> index.html)
    # so the site is a single self-contained file: no fetch(), works opened
    # directly or hosted on GitHub Pages, nothing to configure.
    data_json_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # JSON can legally contain "</script>" inside a string value; escape it so
    # the browser doesn't treat it as the real closing tag.
    data_json_str = data_json_str.replace("</script>", "<\\/script>")

    with open("template.html", "r", encoding="utf-8") as f:
        template = f.read()

    if "__DATA_JSON__" not in template:
        raise SystemExit("template.html is missing the __DATA_JSON__ placeholder")

    html = template.replace("__DATA_JSON__", data_json_str)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print(f"courses: {len(courses)}")
    print(f"with modules: {data['stats']['courses_with_modules']}")
    print(f"total module rows: {data['stats']['total_module_rows']}")
    print(f"institutions: {len(institutions)}")
    print("wrote data.json and index.html")

if __name__ == "__main__":
    main()
