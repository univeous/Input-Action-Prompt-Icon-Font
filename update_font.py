#!/usr/bin/env python3
"""
Kenney Input Prompts Font Updater

1. Checks kenney.nl/assets/input-prompts for a new version
2. Downloads & extracts the zip
3. Merges all TTFs into one (re-assigning PUA codepoints to avoid collisions)
4. Adds a GSUB 'liga' table so typing a glyph's name renders the icon
   e.g. the character sequence x-b-o-x-_-b-u-t-t-o-n-_-a → xbox_button_a glyph

Dependencies: pip install requests beautifulsoup4 fonttools
"""

import json
import re
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables import otTables
from fontTools import ttLib

KENNEY_URL = "https://kenney.nl/assets/input-prompts"
VERSION_FILE = Path(__file__).parent / "kenney_version.json"
OUTPUT_TTF = Path(__file__).parent / "addons/input_prompt_icon_font/icon.ttf"

PUA_START = 0xE000  # where icon codepoints start in the merged font


# ── Version tracking ──────────────────────────────────────────────────────────

def load_stored_version() -> str | None:
    if VERSION_FILE.exists():
        return json.loads(VERSION_FILE.read_text()).get("version")
    return None


def save_version(version: str):
    VERSION_FILE.write_text(json.dumps({"version": version}, indent=2) + "\n")
    print(f"  Saved version → {VERSION_FILE}")


# ── Web: detect version & download URL ───────────────────────────────────────

def fetch_latest() -> tuple[str, str]:
    """Returns (version, zip_url)."""
    print(f"Fetching {KENNEY_URL} ...")
    resp = requests.get(KENNEY_URL, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    zip_url = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"input.?prompts.*\.zip", href, re.I):
            zip_url = href if href.startswith("http") else "https://kenney.nl" + href
            break

    if not zip_url:
        raise RuntimeError("Could not find zip download link on the page.")

    # Version is embedded in the filename, e.g. kenney_input-prompts_1.4.1.zip
    m = re.search(r"_(\d+\.\d+[\.\d]*)\.zip", zip_url)
    version = m.group(1) if m else "unknown"

    print(f"  Latest  : {version}")
    print(f"  Zip URL : {zip_url}")
    return version, zip_url


# ── Download & find TTFs ──────────────────────────────────────────────────────

def download_zip(url: str) -> zipfile.ZipFile:
    print(f"Downloading ...")
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    print(f"  {len(resp.content)/1024:.0f} KB")
    return zipfile.ZipFile(BytesIO(resp.content))


def extract_fonts(zf: zipfile.ZipFile) -> list[TTFont]:
    ttf_entries = sorted(
        [n for n in zf.namelist() if n.lower().endswith(".ttf")],
        key=lambda p: Path(p).name,
    )
    print(f"  {len(ttf_entries)} TTF file(s):")
    fonts = []
    for entry in ttf_entries:
        print(f"    {entry}")
        font = TTFont(BytesIO(zf.read(entry)))
        fonts.append((Path(entry).stem, font))
    return fonts  # list of (stem_name, TTFont)


# ── Merge ─────────────────────────────────────────────────────────────────────

SKIP_GLYPHS = {".notdef", ".null", "nonmarkingreturn", "space"}


def merge_fonts(named_fonts: list[tuple[str, TTFont]]) -> TTFont:
    """
    Collect all icon glyphs from all TTFs into a single TTFont.
    Each TTF reuses PUA from E000, so we reassign codepoints sequentially.
    Returns the merged TTFont (not yet saved).
    """
    base_font = named_fonts[0][1]
    merged = _clone_font(base_font)

    # Clear glyf completely; we'll repopulate only the glyphs we want
    merged["glyf"].glyphs = {}
    merged["glyf"].glyphOrder = []

    cmap_map: dict[int, str] = {}  # new_codepoint → glyph_name
    metrics: dict[str, tuple[int, int]] = {}
    next_cp = PUA_START
    seen: set[str] = set()

    # Always keep .notdef from the base font
    merged["glyf"][".notdef"] = base_font["glyf"][".notdef"]
    adv, lsb = base_font["hmtx"].metrics.get(".notdef", (500, 0))
    metrics[".notdef"] = (adv, lsb)
    seen.add(".notdef")

    for _stem, font in named_fonts:
        src_cmap = font.getBestCmap() or {}
        for cp, gname in src_cmap.items():
            if gname in SKIP_GLYPHS or gname in seen:
                continue
            seen.add(gname)

            try:
                # glyf.__setitem__ appends to its own glyphOrder automatically
                merged["glyf"][gname] = font["glyf"][gname]
            except Exception:
                continue

            adv, lsb = font["hmtx"].metrics.get(gname, (500, 0))
            metrics[gname] = (adv, lsb)
            cmap_map[next_cp] = gname
            next_cp += 1

    # Sync font-level glyph order from glyf (single source of truth)
    merged.setGlyphOrder(merged["glyf"].glyphOrder)
    merged["hmtx"].metrics = metrics

    # Rebuild cmap
    for t in merged["cmap"].tables:
        if t.format in (4, 12):
            t.cmap = {k: v for k, v in cmap_map.items() if k <= 0xFFFF}

    n = len(merged.getGlyphOrder()) - 1  # exclude .notdef
    print(f"  Merged {n} glyphs, codepoints U+{PUA_START:04X}–U+{next_cp-1:04X}")
    return merged, cmap_map


def _clone_font(src: TTFont) -> TTFont:
    buf = BytesIO()
    src.save(buf)
    buf.seek(0)
    return TTFont(buf)


# ── GSUB ligature table ───────────────────────────────────────────────────────

def add_ligatures(font: TTFont, cmap_map: dict[int, str]):
    """
    Add a GSUB 'liga' lookup so that the character sequence of a glyph name
    renders as that icon.  E.g.:
        'x','b','o','x','_','b','u','t','t','o','n','_','a'
        ──────────────────────────────────────────────────→  xbox_button_a glyph

    We also add zero-width glyphs + cmap entries for every ASCII character
    that appears in any glyph name, so the shaper can look them up.
    """
    icon_names = [g for g in font.getGlyphOrder() if g not in SKIP_GLYPHS and g != ".notdef"]

    # Gather all ASCII chars used across glyph names
    needed_chars: set[str] = set()
    for name in icon_names:
        needed_chars.update(name)
    needed_chars = {c for c in needed_chars if c.isascii() and c.isprintable()}

    _ensure_ascii_glyphs(font, needed_chars)

    # Build GSUB
    gsub = _build_gsub(icon_names, font.getGlyphOrder())
    font["GSUB"] = gsub
    print(f"  Added GSUB liga: {len(icon_names)} ligature rules")


def _char_glyph(ch: str) -> str:
    """Standard PostScript glyph name for an ASCII character."""
    from fontTools.agl import UV2AGL
    return UV2AGL.get(ord(ch), f"uni{ord(ch):04X}")


def _ensure_ascii_glyphs(font: TTFont, chars: set[str]):
    """Insert zero-width empty glyphs + cmap entries for ASCII input chars."""
    from fontTools.ttLib.tables._g_l_y_f import Glyph as GlyfGlyph

    existing_order = set(font.getGlyphOrder())
    existing_cmap: dict[int, str] = {}
    for t in font["cmap"].tables:
        existing_cmap.update(t.cmap)

    new_glyphs: list[str] = []
    new_cmap: dict[int, str] = {}

    for ch in sorted(chars):
        cp = ord(ch)
        gname = _char_glyph(ch)
        if gname not in existing_order:
            new_glyphs.append(gname)
            # empty glyph
            g = GlyfGlyph()
            g.numberOfContours = 0
            g.coordinates = None
            font["glyf"][gname] = g
            font["hmtx"].metrics[gname] = (0, 0)  # zero-width
            existing_order.add(gname)
        if cp not in existing_cmap:
            new_cmap[cp] = gname

    if new_glyphs:
        # glyf.__setitem__ already appended to glyf.glyphOrder;
        # sync font-level order from glyf
        font.setGlyphOrder(font["glyf"].glyphOrder)
    if new_cmap:
        for t in font["cmap"].tables:
            if t.format in (4, 12):
                t.cmap.update(new_cmap)


def _build_gsub(icon_names: list[str], all_glyphs: list[str]) -> "ttLib table":
    all_glyph_set = set(all_glyphs)

    # Group ligatures by first glyph for the LigatureSubst subtable
    # first_glyph_name → list of (rest_glyphs, target_glyph)
    by_first: dict[str, list[tuple[list[str], str]]] = {}

    for icon in icon_names:
        if len(icon) < 2:
            continue  # single char; cmap alone is enough
        chars = list(icon)
        first_g = _char_glyph(chars[0])
        rest_g = [_char_glyph(c) for c in chars[1:]]
        if first_g not in all_glyph_set:
            continue
        if any(g not in all_glyph_set for g in rest_g):
            continue
        by_first.setdefault(first_g, []).append((rest_g, icon))

    # Sort longer sequences first (more specific matches take priority)
    for first_g in by_first:
        by_first[first_g].sort(key=lambda x: -len(x[0]))

    # Build the OT structures
    subst = otTables.LigatureSubst()
    subst.Format = 1
    subst.ligatures = {}
    for first_g, entries in by_first.items():
        ligs = []
        for rest_g, target in entries:
            lig = otTables.Ligature()
            lig.Component = rest_g
            lig.LigGlyph = target
            ligs.append(lig)
        subst.ligatures[first_g] = ligs

    lookup = otTables.Lookup()
    lookup.LookupType = 4
    lookup.LookupFlag = 0
    lookup.SubTable = [subst]
    lookup.SubTableIndex = 0

    lookup_list = otTables.LookupList()
    lookup_list.Lookup = [lookup]

    feat = otTables.Feature()
    feat.FeatureParams = None
    feat.LookupListIndex = [0]

    feat_record = otTables.FeatureRecord()
    feat_record.FeatureTag = "liga"
    feat_record.Feature = feat

    feat_list = otTables.FeatureList()
    feat_list.FeatureRecord = [feat_record]
    feat_list.FeatureCount = 1

    def make_script():
        langsys = otTables.DefaultLangSys()
        langsys.ReqFeatureIndex = 0xFFFF
        langsys.FeatureIndex = [0]
        langsys.LookupOrderIndex = None
        script = otTables.Script()
        script.DefaultLangSys = langsys
        script.LangSysRecord = []
        return script

    script_list = otTables.ScriptList()
    script_list.ScriptRecord = []
    for tag in ("DFLT", "latn"):
        sr = otTables.ScriptRecord()
        sr.ScriptTag = tag
        sr.Script = make_script()
        script_list.ScriptRecord.append(sr)
    script_list.ScriptCount = 2

    gsub_table = otTables.GSUB()
    gsub_table.Version = 1.0
    gsub_table.ScriptList = script_list
    gsub_table.FeatureList = feat_list
    gsub_table.LookupList = lookup_list

    wrapper = ttLib.getTableClass("GSUB")()
    wrapper.table = gsub_table
    return wrapper


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    force = "--force" in sys.argv

    stored = load_stored_version()
    latest_version, zip_url = fetch_latest()

    if not force and stored == latest_version:
        print(f"\nAlready up to date (version {latest_version}). Use --force to rebuild.")
        return

    print(f"\n{'Rebuilding' if force else 'New version'}: {stored!r} → {latest_version!r}")

    zf = download_zip(zip_url)

    print("\nExtracting fonts ...")
    named_fonts = extract_fonts(zf)

    print("\nMerging ...")
    merged, cmap_map = merge_fonts(named_fonts)

    print("\nAdding ligatures ...")
    add_ligatures(merged, cmap_map)

    OUTPUT_TTF.parent.mkdir(parents=True, exist_ok=True)
    merged.save(str(OUTPUT_TTF))
    print(f"\nSaved → {OUTPUT_TTF}")

    save_version(latest_version)
    print("Done.")


if __name__ == "__main__":
    main()
