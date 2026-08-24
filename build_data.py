#!/usr/bin/env python3
"""
Build data.json for the IR/Politics course comparison site.

Inputs (in ../output/):
  - ir_courses_final_target_list.csv   master list of 267 target courses
  - modules_flat_bucketed.csv          cleaned, bucketed module rows (subset of courses)

Output:
  - data.json  (in this folder, alongside index.html)

Re-run this after any future scrape + re-clean + re-bucket cycle to refresh the site's data.
"""
import csv
import json
import re
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

def main():
    with open("../output/ir_courses_final_target_list.csv", newline="", encoding="utf-8") as f:
        target = list(csv.DictReader(f))
    with open("../output/modules_flat_bucketed.csv", newline="", encoding="utf-8") as f:
        mods = list(csv.DictReader(f))

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
            bucket_list = [b for b in (m["bucket_1"], m["bucket_2"]) if b and b != "Uncategorized"]
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