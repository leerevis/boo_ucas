#!/usr/bin/env python3
"""
scrape_modules.py
==================

Scrapes module lists (module name / credits / core-or-optional, grouped by
year) for the 267 courses in ir_courses_final_target_list.csv.

Deliberately out of scope (per project decision): fees, UCAS tariff points,
course duration, entry requirements. Those are the same across UK unis /
easy to find manually and would just add noise to the scraper.

WHY requests + BeautifulSoup and not Playwright/Selenium: every page pattern
found so far (including ones that originally looked like they might need JS)
turned out to be present in the plain server-rendered HTML - confirmed by
fetching the raw HTML directly and checking. A page that genuinely needs JS
to render its module list (like UEA's, which has no module content in the
HTML at all) will fail cleanly and land in failed_scrapes.csv rather than
producing garbage.

HOW MODULE EXTRACTION WORKS - "try each known page layout in turn":
UK university course pages do NOT all use the same HTML structure for their
module lists. Five different layouts have been confirmed by hand (see
LAYOUTS below): a standard bullet-point list, Warwick's "name in a paragraph,
credits in a sibling element" style, Portsmouth's "whole module packed into
one heading tag, year on a tab button" style, Leeds' "bold module name at
the start of a long descriptive paragraph" style, and Lancaster's newer
"module name in an accordion-toggle heading, year on a tab, no credits
anywhere on the page" style. Rather than one big set of rules trying to
handle all of these at once (which is fragile - a fix for one site's quirks
risks breaking another), extract_modules() tries each layout in order
against the same parsed page and uses the first one that finds a credible
set of modules. A layout that doesn't match a given page's structure
naturally finds nothing and hands off to the next one.

---------------------------------------------------------------------------
SETUP (run once):
    pip install requests beautifulsoup4 lxml

USAGE:
    # Sanity-check on a random cross-section of ~12 courses (different unis,
    # not just the first ones alphabetically) before committing to a full run
    python scrape_modules.py --sample 12

    # Full run
    python scrape_modules.py

    # Re-run, but only retry rows that failed last time (keeps successes)
    python scrape_modules.py --retry-failed

    # Start completely fresh, ignoring any existing checkpointed output
    python scrape_modules.py --fresh

By default it reads ir_courses_final_target_list.csv from the SAME FOLDER
as this script, and writes its outputs there too:
    modules.json        - one record per course, modules nested by year
    modules_flat.csv     - one row per individual module (for the future
                            module-search / taxonomy pass - dedupe
                            module_name out of this file)
    failed_scrapes.csv   - institution, course_name, url, reason

The script is resumable: it re-reads whatever modules.json /
failed_scrapes.csv already exist, skips URLs already recorded in either
one, and only scrapes what's left. Ctrl-C at any point is safe - progress
is flushed to disk after every course. Use --retry-failed to specifically
re-attempt only the ones that failed before, or --fresh to ignore existing
output and start over from nothing.
---------------------------------------------------------------------------
"""

import argparse
import csv
import json
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests beautifulsoup4 lxml")

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests beautifulsoup4 lxml")


# ---------------------------------------------------------------------------
# Paths / defaults
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = SCRIPT_DIR / "ir_courses_final_target_list.csv"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MIN_MODULES_FOR_SUCCESS = 3   # below this -> failed, not "a thin partial result"

# ---------------------------------------------------------------------------
# Shared heuristic patterns (used by every layout below)
# ---------------------------------------------------------------------------

NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5,
}

YEAR_RE_A = re.compile(r"\b(year|stage|part|level)\s*([1-5]|one|two|three|four|five)\b", re.I)
YEAR_RE_B = re.compile(r"\b(1st|2nd|3rd|4th|5th|first|second|third|fourth|fifth)\s*year\b", re.I)

CORE_RE = re.compile(r"\b(core|compulsory|required)\s*(modules?|courses?|units?)?\b", re.I)
OPTIONAL_RE = re.compile(r"\b(option(al)?|elective)s?\s*(modules?|courses?|units?)?\b", re.I)

CREDITS_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(credits?|cats\b|ects\b)", re.I)

# Headings that mean "the module list has ended" - hitting one of these
# CLOSES the current year/type context, forcing a fresh Year/Stage heading
# before any more list items are accepted. Without this, an unrecognized
# heading (e.g. "Entry requirements", "Feedback", "Learning and teaching")
# would otherwise just be ignored and the script would keep vacuuming up
# every subsequent list item on the page - grade requirements, scholarship
# lists, employer/destination lists, teaching-methods lists - all
# mislabelled as belonging to the last real year of modules.
STOP_HEADING_RE = re.compile(
    r"\b(entry requirements?|how to apply|admissions?|application (process|deadline)s?|"
    r"tuition fees?|fees (and|&) funding|funding|scholarships?|bursar(y|ies)|"
    r"careers?( prospects)?|graduate (destinations?|outcomes?|prospects)|"
    r"student (profiles?|stories|destinations?)|what our students say|"
    r"why (study|choose)|course overview|related (courses?|degrees?)|"
    r"contact (us|information)|open days?|accreditation|"
    r"learning (and|&) teaching( methods)?|teaching (and|&) learning( methods)?|"
    r"learning methods?|teaching methods?|how you('ll| will) learn|"
    r"teaching (and|&) assessment|assessment methods?|assessment\b|feedback|"
    r"international students?|essential subjects?|"
    r"UCAS (code|points|tariff)|how you('ll| will) be assessed|"
    r"employability|placements? (and|&) (careers|internships)|"
    r"you may also like|discover more)\b",
    re.I,
)

BLACKLIST_RE = re.compile(
    r"\b(privacy|cookie|terms (and|&) conditions|accessibility|sitemap|"
    r"copyright|all rights reserved|skip to (main )?content|open days?|"
    r"book a (visit|tour)|apply now|contact us|read more|find out more|"
    r"for more information|"
    r"share this (page|article)|back to top|log ?in|sign ?in|"
    r"navigation|main menu|follow us|subscribe|newsletter|"
    r"student (login|portal)|clearing|virtual (tour|open day)|"
    r"related (course|degree)s?|you (might|may) also like|"
    r"you('ll| will) study|if you study|"
    r"key (facts|information)|entry requirements?|tuition fees?|"
    r"ucas (code|points|tariff)|how to apply|"
    r"entry grades?|minimum entry|standard entry|"
    r"\bIB points?\b|\(HL \d|A-?Levels? (standard|minimum)|"
    r"\bGCSEs?\b|\bEPQs?\b|\bBTECs?\b|\bT[- ]?Levels?\b|"
    r"\bA[- ]?Levels?\b|\bInternational Baccalaureate\b|\bcore maths\b|"
    r"\bextended essay\b|"
    r"salary|scholarships?|bursar(y|ies)|"
    r"£\s?\d|per year of study|"
    r"student destinations?|graduate destinations?|"
    r"placements? (and|&) careers)\b",
    re.I,
)

# List items that read like learning-outcome bullet points ("define academic
# integrity and academic misconduct", "explain why and when you should
# reference...") rather than module titles. These start with a bare
# command-form verb and read as a full sentence - genuine module titles
# almost never do both at once.
LEARNING_OUTCOME_VERB_RE = re.compile(
    r"^(understand|explain|define|describe|identify|demonstrate|achieve|"
    r"communicate|explore|analyse|analyze|evaluate|apply|provide|develop|"
    r"discuss|examine|assess|recognise|recognize|outline|summarise|summarize)\b",
    re.I,
)

# Bare teaching-method labels (e.g. "Lectures", "Group work", "Independent
# study") that sometimes appear as plain bullet/list items in a "teaching
# and learning" / "learning methods" section. STOP_HEADING_RE is meant to
# close that section off before any of its items get vacuumed up as
# modules, but real pages use so many different phrasings for that heading
# ("Learning Methods", "Teaching and learning", ...) that it's safer to
# also reject the bare item text directly, as a backstop. This is a strict
# WHOLE-STRING match (anchored start to end), not a substring search - a
# real module whose title happens to contain one of these words, e.g.
# "Features Journalism Workshop", is much longer than the bare phrase and
# will not match.
TEACHING_METHOD_ONLY_RE = re.compile(
    r"^(ai learning|lectures?|seminars?|tutorials?|workshops?|"
    r"practical( session)?s?|placements?|fieldwork|"
    r"group work|group projects?|individual projects?|"
    r"independent (study|learning)|self[- ]stud(y|ies)|"
    r"directed study|guided study|contact hours?|"
    r"scheduled learning( and teaching)?|independent learning|"
    r"blended learning|online learning|in-person( teaching)?|"
    r"face-to-face( teaching)?|synchronous( sessions?)?|asynchronous( sessions?)?|"
    r"small[- ]group (teaching|work|sessions?)|large[- ]group (teaching|sessions?))"
    r"\s*$",
    re.I,
)

# Assessment-method percentage breakdowns (e.g. "0% practical exams", "20%
# coursework") that show up as short standalone lines near a module list.
# Anchored whole-string, same reasoning as TEACHING_METHOD_ONLY_RE above.
ASSESSMENT_BREAKDOWN_RE = re.compile(
    r"^\s*\d{1,3}\s*%\s*(coursework|practical(\s+exams?)?|written(\s+exams?)?|"
    r"exams?|examinations?|tests?|presentations?)\s*$",
    re.I,
)

LEADING_BULLET_RE = re.compile(r"^[\-•\*•●■]+\s*")
LEADING_NUMBER_RE = re.compile(r"^\d+[\.\)]\s*")
TRAILING_CREDITS_RE = re.compile(
    r"\s*[\(\[]?\b\d{1,3}(?:\.\d+)?\s*(credits?|cats|ects)\b[\)\]]?\s*$", re.I
)
DESCRIPTION_SPLIT_RE = re.compile(r"\s*[:–—-]\s+")

HEADING_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "dt"]
ITEM_TAGS = ["li", "dd"]
NOISE_TAGS = ["script", "style", "nav", "header", "footer", "form", "svg", "noscript"]


def normalize_year_label(text):
    m = YEAR_RE_A.search(text)
    if m:
        raw = m.group(2).lower()
        n = int(raw) if raw.isdigit() else NUM_WORDS.get(raw)
        if n:
            return f"Year {n}"
    m = YEAR_RE_B.search(text)
    if m:
        n = NUM_WORDS.get(m.group(1).lower())
        if n:
            return f"Year {n}"
    return None


def looks_like_heading_text(text):
    """Short text is required for something to count as a Year/Core/Optional
    heading - a long paragraph that happens to mention 'year 2' in passing
    should NOT be treated as a heading."""
    return len(text) <= 60


def is_module_candidate(text):
    t = text.strip()
    if not (3 <= len(t) <= 150):
        return False
    if BLACKLIST_RE.search(t):
        return False
    if t.isupper() and len(t.split()) <= 2:
        return False
    if re.match(r"^(https?://|www\.)", t, re.I):
        return False
    # reject things that are almost entirely punctuation/whitespace
    if not re.search(r"[A-Za-z]{3,}", t):
        return False
    # reject learning-outcome-style bullet points (see LEARNING_OUTCOME_VERB_RE)
    if LEARNING_OUTCOME_VERB_RE.match(t) and (len(t) > 40 or t.rstrip().endswith(".")):
        return False
    # reject bare teaching-method labels and assessment-percentage lines
    # that leak in from an unrecognized "teaching and learning" section
    # heading (see TEACHING_METHOD_ONLY_RE / ASSESSMENT_BREAKDOWN_RE)
    if TEACHING_METHOD_ONLY_RE.match(t):
        return False
    if ASSESSMENT_BREAKDOWN_RE.match(t):
        return False
    return True


def clean_module_name(text):
    # collapse any internal whitespace runs (including literal newlines from
    # how the source HTML happens to be line-wrapped) down to single spaces
    t = re.sub(r"\s+", " ", text).strip()
    t = LEADING_BULLET_RE.sub("", t)
    t = LEADING_NUMBER_RE.sub("", t)
    t = TRAILING_CREDITS_RE.sub("", t)

    # Some pages glue a full-sentence blurb onto the module title in the same
    # element, e.g. "Concepts in Global Politics : introduces students to the
    # foundational features...". Split on the first ':'/'-' and drop the
    # trailing part IF it reads like a description (starts lowercase, is
    # long, and/or ends in a full stop) rather than a genuine subtitle like
    # "US Foreign Policy: The Dilemma of Power" (starts uppercase, short).
    m = DESCRIPTION_SPLIT_RE.search(t)
    if m:
        head, tail = t[: m.start()], t[m.end():]
        if head and tail:
            looks_like_blurb = (
                tail[:1].islower()
                or len(tail) > 60
                or tail.rstrip().endswith(".")
            )
            if looks_like_blurb:
                t = head
                # Trimming the blurb can expose a trailing "(NN credits)" that
                # was previously in the middle of the string, e.g. "Module M
                # (20 credits) - long description..." -> after the blurb cut,
                # "Module M (20 credits)". TRAILING_CREDITS_RE already ran
                # once above (before this split), so it never got a chance
                # to see this newly-exposed trailing credits parenthetical.
                # Re-apply it now.
                t = TRAILING_CREDITS_RE.sub("", t).strip()

    # Only drop a genuinely empty leftover "()" - do NOT blindly strip
    # trailing/leading parens, or a legitimately-parenthesised ending like
    # "International Relations Theory and Planet Politics (Semester One)"
    # loses its closing bracket, becoming "...(Semester One" - real bug,
    # found in real scraped output.
    t = re.sub(r"\(\s*\)\s*$", "", t)
    t = t.strip(" -–—:")
    return t.strip()


def extract_credits(text):
    m = CREDITS_RE.search(text)
    if not m:
        return None
    val = m.group(1)
    try:
        f = float(val)
        return int(f) if f.is_integer() else f
    except ValueError:
        return None


def _finalize(years, any_year_seen):
    """Shared success/failure/warnings logic used by every layout."""
    total_modules = sum(len(v) for v in years.values())

    if not any_year_seen:
        return {}, [], "no year/stage headings found"

    if total_modules < MIN_MODULES_FOR_SUCCESS:
        return {}, [], "year headings found but no usable module list detected"

    warnings = []
    empty_years = [y for y, mods in years.items() if not mods]
    if empty_years:
        warnings.append(f"no modules found under: {', '.join(sorted(empty_years))}")
        for y in empty_years:
            del years[y]
    if len(years) == 1:
        warnings.append("only one year of modules found - course may span more years than detected")
    modules_missing_credits = sum(
        1 for mods in years.values() for m in mods if m["credits"] is None
    )
    if modules_missing_credits and total_modules and modules_missing_credits / total_modules > 0.5:
        warnings.append("credit values not detected for most modules")
    modules_missing_type = sum(
        1 for mods in years.values() for m in mods if m["type"] is None
    )
    if modules_missing_type == total_modules:
        warnings.append("core/optional split not detected")

    return years, warnings, None


# ---------------------------------------------------------------------------
# Layout 1: standard bullet-point list
# <h2>Year 1</h2><h3>Core modules</h3><ul><li>Module Name (20 credits)</li>...
# The majority pattern - tried first.
# ---------------------------------------------------------------------------

def try_layout_bullet_list(main):
    flat = main.find_all(HEADING_TAGS + ITEM_TAGS + ["p"])

    years = {}
    current_year = None
    current_type = None
    any_year_seen = False

    def add_module(name_text):
        name = clean_module_name(name_text)
        if not name:
            return
        credits = extract_credits(name_text)
        years.setdefault(current_year, []).append({
            "name": name, "credits": credits, "type": current_type,
        })

    for tag in flat:
        text = tag.get_text(" ", strip=True)
        if not text:
            continue

        if tag.name in HEADING_TAGS:
            if looks_like_heading_text(text):
                y = normalize_year_label(text)
                if y:
                    current_year = y
                    current_type = None
                    any_year_seen = True
                    continue
                if CORE_RE.search(text):
                    current_type = "core"
                    continue
                if OPTIONAL_RE.search(text):
                    current_type = "optional"
                    continue
                if STOP_HEADING_RE.search(text):
                    current_year = None
                    current_type = None
                    continue
            continue

        if tag.name in ITEM_TAGS:
            # skip sub-bullets nested inside another list item - these are
            # almost always part of ONE module's description (e.g. a list of
            # topics/case-studies covered), not separate top-level modules
            if tag.find_parent(["li", "dd"]) is not None:
                continue
            if current_year is None:
                continue
            # a bare "Year N" list item is a mislabelled tab/nav marker
            # (some sites build their year-switcher out of <li> tags), not
            # a module - use it to update the year context instead
            if len(text) <= 15:
                y = normalize_year_label(text)
                if y:
                    current_year = y
                    current_type = None
                    any_year_seen = True
                    continue
            if is_module_candidate(text):
                add_module(text)
            continue

        if tag.name == "p":
            if looks_like_heading_text(text):
                y = normalize_year_label(text)
                if y:
                    current_year = y
                    current_type = None
                    any_year_seen = True
                    continue
                if CORE_RE.search(text):
                    current_type = "core"
                    continue
                if OPTIONAL_RE.search(text):
                    current_type = "optional"
                    continue
                if STOP_HEADING_RE.search(text):
                    current_year = None
                    current_type = None
                    continue
            # a <p> is only trusted as a module line if it's short-ish AND
            # carries a credits value - otherwise it's almost certainly
            # prose description text, not a module list
            if current_year is not None and len(text) <= 150 and CREDITS_RE.search(text) \
                    and is_module_candidate(text):
                add_module(text)
            continue

    return _finalize(years, any_year_seen)


# ---------------------------------------------------------------------------
# Layout 2: paragraph name + course-code-encoded credits (Warwick-style)
# <p>Introduction to Politics (PO107-30)</p><div class="module--cats">30</div>
# The module name is a plain <p>, ending in a course-code parenthetical
# whose suffix after the last hyphen is the credit value.
# ---------------------------------------------------------------------------

CODE_CREDIT_RE = re.compile(r"^(.*?)\s*\([A-Za-z0-9/]{2,10}-(\d{1,3})\)\s*$", re.DOTALL)


def try_layout_paragraph_sibling_credits(main):
    flat = main.find_all(HEADING_TAGS + ["p"])

    years = {}
    current_year = None
    current_type = None
    any_year_seen = False

    for tag in flat:
        text = tag.get_text(" ", strip=True)
        if not text:
            continue

        if tag.name in HEADING_TAGS:
            if looks_like_heading_text(text):
                y = normalize_year_label(text)
                if y:
                    current_year = y
                    current_type = None
                    any_year_seen = True
                    continue
                if CORE_RE.search(text):
                    current_type = "core"
                    continue
                if OPTIONAL_RE.search(text):
                    current_type = "optional"
                    continue
                if STOP_HEADING_RE.search(text):
                    current_year = None
                    current_type = None
                    continue
            continue

        if tag.name == "p" and current_year is not None:
            m = CODE_CREDIT_RE.match(text)
            if not m:
                continue
            name = clean_module_name(m.group(1))
            if name and is_module_candidate(name):
                years.setdefault(current_year, []).append({
                    "name": name, "credits": int(m.group(2)), "type": current_type,
                })

    return _finalize(years, any_year_seen)


# ---------------------------------------------------------------------------
# Layout 3: module packed into a heading tag (Portsmouth-style)
# Year label lives on a <button> tab control, not a heading, and each
# individual module is itself a heading whose text is the whole
# "Name - NN credits <description...>" string.
#
# IMPORTANT: on a real accessible-tabs widget (confirmed on Portsmouth's
# site), all the tab BUTTONS sit together at the top of the section, and
# the tab PANELS (one per year) all follow afterwards, one after another -
# they are NOT interleaved in document order. A naive "last button/heading
# seen wins" walk therefore mislabels every module under whichever tab
# button happens to appear last (tested and confirmed - it put everything
# under "Year 3"). The fix is to use the standard WAI-ARIA tabs wiring:
# each button has an aria-controls="<panel id>" attribute pointing at its
# matching panel, so match year labels to panels explicitly via that
# attribute and scan each panel independently.
# ---------------------------------------------------------------------------

MODULE_HEADING_RE = re.compile(r"^(.*?)\s*-\s*(\d{1,3})\s*credits?\b", re.I | re.DOTALL)


def _find_year_panels(main):
    """
    Find (year_label, panel_element) pairs via the standard WAI-ARIA tabs
    pattern: a button/link with aria-controls="<panel id>" whose visible
    text is a year label, matched to the panel element it controls. Shared
    by any layout that needs to know which content belongs to which year
    when the year lives on a tab control rather than a heading (Portsmouth,
    Lancaster's newer template, and presumably others).
    """
    year_panels = []
    for btn in main.find_all(["button", "a"], attrs={"aria-controls": True}):
        text = btn.get_text(" ", strip=True)
        if not looks_like_heading_text(text):
            continue
        y = normalize_year_label(text)
        if not y:
            continue
        panel = main.find(id=btn.get("aria-controls"))
        if panel is not None:
            year_panels.append((y, panel))
    return year_panels


def _scan_module_headings(container, year, years):
    """Scan one panel/container for Core/Optional sub-headings and modules
    packed into heading tags, attaching everything found to `year`."""
    current_type = None
    for tag in container.find_all(HEADING_TAGS):
        text = tag.get_text(" ", strip=True)
        if not text:
            continue
        if looks_like_heading_text(text):
            if CORE_RE.search(text):
                current_type = "core"
                continue
            if OPTIONAL_RE.search(text):
                current_type = "optional"
                continue
            if STOP_HEADING_RE.search(text):
                current_type = None
                continue
        m = MODULE_HEADING_RE.match(text)
        if m:
            name = clean_module_name(m.group(1))
            if name and is_module_candidate(name):
                years.setdefault(year, []).append({
                    "name": name, "credits": int(m.group(2)), "type": current_type,
                })


def try_layout_heading_module(main):
    years = {}

    # preferred path: match each year-tab button to its panel via
    # aria-controls, then scan each panel on its own
    year_panels = _find_year_panels(main)

    if year_panels:
        for y, panel in year_panels:
            _scan_module_headings(panel, y, years)
        return _finalize(years, any_year_seen=True)

    # fallback: no aria-controls tab wiring found on this page - fall back
    # to a simple sequential walk (button labels + headings in document
    # order). Less reliable if buttons and panels aren't interleaved, but
    # better than finding nothing.
    flat = main.find_all(HEADING_TAGS + ["button"])
    current_year = None
    current_type = None
    any_year_seen = False

    for tag in flat:
        text = tag.get_text(" ", strip=True)
        if not text:
            continue

        if tag.name == "button":
            if looks_like_heading_text(text):
                y = normalize_year_label(text)
                if y:
                    current_year = y
                    current_type = None
                    any_year_seen = True
            continue

        if looks_like_heading_text(text):
            y = normalize_year_label(text)
            if y:
                current_year = y
                current_type = None
                any_year_seen = True
                continue
            if CORE_RE.search(text):
                current_type = "core"
                continue
            if OPTIONAL_RE.search(text):
                current_type = "optional"
                continue
            if STOP_HEADING_RE.search(text):
                current_year = None
                current_type = None
                continue

        if current_year is not None:
            m = MODULE_HEADING_RE.match(text)
            if m:
                name = clean_module_name(m.group(1))
                if name and is_module_candidate(name):
                    years.setdefault(current_year, []).append({
                        "name": name, "credits": int(m.group(2)), "type": current_type,
                    })

    return _finalize(years, any_year_seen)


# ---------------------------------------------------------------------------
# Layout 4: bold module name inside a long paragraph (Leeds-style)
# <p><strong>International Politics</strong> (20 credits) - This module
# introduces you to... [long description]</p>
# The module name is bolded at the very start of the paragraph; everything
# after the credits parenthetical is description and gets discarded
# regardless of length.
# ---------------------------------------------------------------------------

BOLD_CREDIT_RE = re.compile(r"^\s*\(\s*(\d{1,3})\s*credits?\s*\)", re.I)


def try_layout_bold_paragraph(main):
    flat = main.find_all(HEADING_TAGS + ["p"])

    years = {}
    current_year = None
    current_type = None
    any_year_seen = False

    for tag in flat:
        text = tag.get_text(" ", strip=True)
        if not text:
            continue

        if tag.name in HEADING_TAGS:
            if looks_like_heading_text(text):
                y = normalize_year_label(text)
                if y:
                    current_year = y
                    current_type = None
                    any_year_seen = True
                    continue
                if CORE_RE.search(text):
                    current_type = "core"
                    continue
                if OPTIONAL_RE.search(text):
                    current_type = "optional"
                    continue
                if STOP_HEADING_RE.search(text):
                    current_year = None
                    current_type = None
                    continue
            continue

        if tag.name != "p":
            continue

        bold = tag.find(["strong", "b"])
        if bold is None:
            continue
        bold_text = bold.get_text(" ", strip=True)
        if not bold_text:
            continue

        # is the bold text itself a structural sub-heading, e.g.
        # "<p><strong>Compulsory modules</strong></p>"?
        if looks_like_heading_text(bold_text):
            y = normalize_year_label(bold_text)
            if y:
                current_year = y
                current_type = None
                any_year_seen = True
                continue
            if CORE_RE.search(bold_text):
                current_type = "core"
                continue
            if OPTIONAL_RE.search(bold_text):
                current_type = "optional"
                continue
            if STOP_HEADING_RE.search(bold_text):
                current_year = None
                current_type = None
                continue

        if current_year is None:
            continue

        # text immediately following the bold run, within the full paragraph
        idx = text.find(bold_text)
        rest = text[idx + len(bold_text):] if idx != -1 else ""
        m = BOLD_CREDIT_RE.match(rest)
        if not m:
            continue
        name = clean_module_name(bold_text)
        if name and is_module_candidate(name):
            years.setdefault(current_year, []).append({
                "name": name, "credits": int(m.group(1)), "type": current_type,
            })

    return _finalize(years, any_year_seen)


# ---------------------------------------------------------------------------
# Layout 5: module name in an accordion-toggle heading, year on a tab
# (Lancaster's newer template)
# Year lives on a tab link (same aria-controls wiring as Portsmouth/Layout
# 3 - reuses _find_year_panels). Inside each year panel, every module is a
# heading whose ENTIRE content is a single clickable accordion/dropdown
# toggle, e.g. <h3><button aria-expanded="false" aria-controls="panel-id">
# Origins and Foundations of International Relations</button></h3> - no
# credits value anywhere on the page at all, not even in the expanded
# panel content (confirmed by hand). Tried last: it's the most permissive
# layout (any non-structural heading inside a matched year panel counts as
# a module), so it only ever runs once every more specific layout has
# already failed to find enough modules on the page.
# ---------------------------------------------------------------------------

def _looks_like_accordion_toggle(tag):
    """A heading whose entire content is a single clickable accordion/
    dropdown toggle control (a <button> or <a> carrying aria-expanded
    and/or aria-controls) - the standard accessible-accordion pattern.
    Lets us tell "this heading IS a collapsible module name" apart from an
    ordinary structural heading like Year/Core/Optional, without keying
    off any site-specific class names."""
    toggle = tag.find(["button", "a"])
    if toggle is None:
        return False
    return toggle.has_attr("aria-expanded") or toggle.has_attr("aria-controls")


def try_layout_accordion_module(main):
    year_panels = _find_year_panels(main)
    if not year_panels:
        return _finalize({}, any_year_seen=False)

    years = {}
    for year, panel in year_panels:
        current_type = None
        for tag in panel.find_all(HEADING_TAGS):
            text = tag.get_text(" ", strip=True)
            if not text:
                continue

            is_toggle = _looks_like_accordion_toggle(tag)

            if looks_like_heading_text(text) and not is_toggle:
                if CORE_RE.search(text):
                    current_type = "core"
                    continue
                if OPTIONAL_RE.search(text):
                    current_type = "optional"
                    continue
                if STOP_HEADING_RE.search(text):
                    current_type = None
                    continue
                # any other short, plain (non-toggle) heading inside the
                # panel is structural noise (e.g. a panel sub-title), not
                # a module
                continue

            if is_toggle:
                name = clean_module_name(text)
                if name and is_module_candidate(name):
                    years.setdefault(year, []).append({
                        "name": name, "credits": extract_credits(text), "type": current_type,
                    })

    return _finalize(years, any_year_seen=True)


# ---------------------------------------------------------------------------
# Orchestrator - try each layout in turn, use the first one that succeeds
# ---------------------------------------------------------------------------

LAYOUTS = [
    ("bullet_list", try_layout_bullet_list),
    ("paragraph_credits", try_layout_paragraph_sibling_credits),
    ("heading_module", try_layout_heading_module),
    ("bold_paragraph", try_layout_bold_paragraph),
    ("accordion_module", try_layout_accordion_module),
]


def _select_main_region(soup):
    """
    Narrow the parsed page down to its main content region, falling back to
    the whole document if nothing obviously qualifies.

    A page WITH a real <main> tag (or role="main") is the easy, reliable
    case. Without one, the old fallback took the FIRST element whose id
    happened to contain "content" - which on Aston's course pages (and
    presumably others using the same common accessibility pattern) is an
    empty "skip to content" anchor link sitting right at the top of the
    page: <a id="main-content"></a>, zero text, nowhere near the actual
    module list. That produced "no year/stage headings found" even though
    the real content was sitting right there in the HTML, just outside the
    (wrong) region searched.

    Fix: gather every id="...content..."/<article> candidate, throw out
    any with barely any text (skip links, empty wrappers), and pick
    whichever real candidate has the most text - not just the first one
    in document order.
    """
    main = soup.find("main")
    if main is not None:
        return main
    main = soup.find(attrs={"role": "main"})
    if main is not None:
        return main

    candidates = soup.find_all(id=re.compile("content", re.I)) + soup.find_all("article")
    # a genuine content region has real prose in it; a skip-link or empty
    # wrapper div does not - 200 chars is a low bar deliberately, just
    # enough to rule out near-empty elements
    candidates = [c for c in candidates if len(c.get_text(strip=True)) > 200]
    if candidates:
        return max(candidates, key=lambda c: len(c.get_text(strip=True)))

    return soup


def extract_modules(html):
    """
    Returns (years_dict, warnings, reason_if_failed_or_None).
    years_dict: {"Year 1": [{"name":..., "credits":..., "type":...}, ...], ...}

    Tries each known page layout (see LAYOUTS) against the same parsed page,
    in order, and returns the first one that finds a credible set of
    modules. A layout that doesn't match a given page's structure naturally
    finds nothing and hands off to the next one.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(NOISE_TAGS):
        tag.decompose()

    main = _select_main_region(soup)

    saw_any_year_heading = False
    for _layout_name, layout_fn in LAYOUTS:
        years, warnings, reason = layout_fn(main)
        if reason is None:
            return years, warnings, None
        if reason != "no year/stage headings found":
            saw_any_year_heading = True

    if saw_any_year_heading:
        return {}, [], "year headings found but no usable module list detected"
    return {}, [], "no year/stage headings found"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch(session, url, timeout, max_retries):
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)
        except requests.exceptions.Timeout:
            last_err = "timeout"
        except requests.exceptions.ConnectionError:
            last_err = "connection error"
        except requests.exceptions.RequestException as e:
            last_err = f"request error: {e}"
        else:
            if resp.status_code == 200:
                ctype = resp.headers.get("Content-Type", "")
                if "html" not in ctype and ctype:
                    return None, f"non-HTML response ({ctype})"
                return resp.text, None
            if resp.status_code in (403, 406, 429):
                last_err = f"blocked (HTTP {resp.status_code})"
            elif resp.status_code == 404:
                return None, "HTTP 404"
            else:
                last_err = f"HTTP {resp.status_code}"
        if attempt < max_retries:
            time.sleep(1.5 * (attempt + 1))
    return None, last_err or "unknown error"


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_input_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_existing(output_dir):
    modules_path = output_dir / "modules.json"
    failed_path = output_dir / "failed_scrapes.csv"
    modules = {}
    failed = {}
    if modules_path.exists():
        try:
            data = json.load(open(modules_path, encoding="utf-8"))
            for rec in data:
                modules[rec["url"]] = rec
        except (json.JSONDecodeError, KeyError):
            pass
    if failed_path.exists():
        try:
            with open(failed_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    failed[row["url"]] = row
        except (csv.Error, KeyError):
            pass
    return modules, failed


def _write_with_retry(path, write_fn, retries=6, initial_delay=0.5):
    """
    Open `path` for writing and call write_fn(file_handle), retrying with
    backoff if the OS reports the file is locked (PermissionError).

    This is aimed squarely at running inside a OneDrive-synced folder on
    Windows: the moment this script (over)writes one of its output files,
    OneDrive's sync process can grab a brief lock on it, and the very next
    flush (which happens after every single course) can hit that lock and
    raise PermissionError / WinError 32. The lock is normally gone within a
    second or two, so a short retry-with-backoff loop rides straight through
    it without losing any progress. If something is holding the file open
    for longer than that - most commonly the file being open in Excel - the
    retries are exhausted and a clear, actionable error is raised instead of
    a raw traceback.
    """
    delay = initial_delay
    last_err = None
    for attempt in range(retries):
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                write_fn(f)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(delay)
            delay *= 2
    raise PermissionError(
        f"Could not write to {path} after {retries} attempts. It's most likely "
        f"open in Excel (or another program) right now - close it and re-run "
        f"the script. Nothing has been lost: already-recorded courses are "
        f"skipped automatically, so it'll pick up right where it left off."
    ) from last_err


def write_outputs(output_dir, modules_by_url, failed_by_url, row_order):
    modules_path = output_dir / "modules.json"
    flat_path = output_dir / "modules_flat.csv"
    failed_path = output_dir / "failed_scrapes.csv"

    ordered_records = [modules_by_url[u] for u in row_order if u in modules_by_url]
    ordered_failed = [failed_by_url[u] for u in row_order if u in failed_by_url]

    def write_modules(f):
        json.dump(ordered_records, f, indent=1, ensure_ascii=False)

    def write_flat(f):
        w = csv.writer(f)
        w.writerow(["institution", "course_name", "url", "year", "module_name", "credits", "type"])
        for rec in ordered_records:
            for year, mods in rec.get("years", {}).items():
                for m in mods:
                    w.writerow([
                        rec["institution"], rec["course_name"], rec["url"],
                        year, m["name"], m["credits"] if m["credits"] is not None else "",
                        m["type"] or "",
                    ])

    def write_failed(f):
        w = csv.writer(f)
        w.writerow(["institution", "course_name", "url", "reason"])
        for row in ordered_failed:
            w.writerow([row["institution"], row["course_name"], row["url"], row["reason"]])

    _write_with_retry(modules_path, write_modules)
    _write_with_retry(flat_path, write_flat)
    _write_with_retry(failed_path, write_failed)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Scrape module lists for IR course pages.")
    ap.add_argument("--input", default=str(DEFAULT_INPUT), help="input CSV (target list)")
    ap.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="where to write outputs")
    ap.add_argument("--limit", type=int, default=None, help="only process the first N rows (for testing)")
    ap.add_argument("--sample", type=int, default=None,
                     help="randomly pick N rows from across the whole list instead of the first N "
                          "(better than --limit for a quick sanity check, since it isn't all one university)")
    ap.add_argument("--delay", type=float, default=1.5, help="base delay in seconds between requests")
    ap.add_argument("--timeout", type=float, default=15.0, help="per-request timeout in seconds")
    ap.add_argument("--max-retries", type=int, default=2, help="retries per URL on transient failure")
    ap.add_argument("--retry-failed", action="store_true",
                     help="re-attempt rows previously recorded as failed (keeps existing successes)")
    ap.add_argument("--fresh", action="store_true",
                     help="ignore any existing modules.json / failed_scrapes.csv and start over")
    args = ap.parse_args()

    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = load_input_rows(input_path)
    if args.sample:
        n = min(args.sample, len(rows))
        rows = random.sample(rows, n)
        print(f"Randomly sampled {n} rows from across the full list.")
    elif args.limit:
        rows = rows[: args.limit]
    row_order = [r["url"] for r in rows]

    if args.fresh:
        modules_by_url, failed_by_url = {}, {}
    else:
        modules_by_url, failed_by_url = load_existing(output_dir)

    if args.retry_failed:
        for url in list(failed_by_url.keys()):
            failed_by_url.pop(url, None)

    to_process = [
        r for r in rows
        if r["url"] not in modules_by_url and r["url"] not in failed_by_url
    ]

    print(f"Input rows: {len(rows)}")
    print(f"Already recorded (skipping): {len(rows) - len(to_process)}")
    print(f"To scrape this run: {len(to_process)}")
    if not to_process:
        print("Nothing to do. Use --retry-failed or --fresh to re-run.")
        return

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    for i, row in enumerate(to_process, 1):
        institution = row["institution"]
        course_name = row["course_name"]
        url = row["url"]
        domain = urlparse(url).netloc

        print(f"[{i}/{len(to_process)}] {institution} - {course_name} ({domain})", end=" ... ", flush=True)

        html, err = fetch(session, url, args.timeout, args.max_retries)

        if html is None:
            failed_by_url[url] = {"institution": institution, "course_name": course_name,
                                   "url": url, "reason": err}
            modules_by_url.pop(url, None)
            print(f"FAILED ({err})")
        else:
            years, warnings, reason = extract_modules(html)
            if reason:
                failed_by_url[url] = {"institution": institution, "course_name": course_name,
                                       "url": url, "reason": reason}
                modules_by_url.pop(url, None)
                print(f"FAILED ({reason})")
            else:
                status = "partial" if warnings else "ok"
                total = sum(len(v) for v in years.values())
                modules_by_url[url] = {
                    "institution": institution,
                    "course_name": course_name,
                    "url": url,
                    "scrape_status": status,
                    "warnings": warnings,
                    "years": years,
                }
                failed_by_url.pop(url, None)
                flag = " [partial]" if warnings else ""
                print(f"ok - {total} modules{flag}")

        # flush after every course so Ctrl-C never loses progress
        write_outputs(output_dir, modules_by_url, failed_by_url, row_order)

        if i < len(to_process):
            time.sleep(args.delay + random.uniform(0, args.delay * 0.4))

    n_ok = sum(1 for r in modules_by_url.values() if r["scrape_status"] == "ok")
    n_partial = sum(1 for r in modules_by_url.values() if r["scrape_status"] == "partial")
    n_failed = len(failed_by_url)
    print()
    print("Done.")
    print(f"  ok:      {n_ok}")
    print(f"  partial: {n_partial}  (some modules found but flagged - see 'warnings' in modules.json)")
    print(f"  failed:  {n_failed}  (see failed_scrapes.csv)")
    print(f"Wrote: {output_dir / 'modules.json'}, {output_dir / 'modules_flat.csv'}, {output_dir / 'failed_scrapes.csv'}")


if __name__ == "__main__":
    main()