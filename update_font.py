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


def normalize_font(font: TTFont):
    """
    Scale and translate all icon glyphs so they visually fill the em square,
    fixing size and vertical-centering regressions when Kenney updates the
    source fonts with a different design space (e.g. UPM changed from 1024→2048
    in v1.4.1 while glyph coordinates only used ~44 % of the new em height).

    Strategy
    --------
    1. Find the maximum advance width across all icon glyphs (src_adv).
    2. Derive a uniform scale so that advance becomes equal to UPM (square em).
    3. Center the scaled glyphs vertically within the em.
    4. Update hhea / OS/2 / head metrics to match the new extents.
    """
    from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates

    glyf_tbl = font["glyf"]
    hmtx = font["hmtx"]
    upm = font["head"].unitsPerEm

    skip = set(SKIP_GLYPHS) | {".notdef"}
    icon_names = [g for g in font.getGlyphOrder() if g not in skip]

    # ── 1. Survey current extents ────────────────────────────────────────────
    advances, ymins, ymaxs = [], [], []
    for name in icon_names:
        adv, _ = hmtx.metrics.get(name, (0, 0))
        if adv > 0:
            advances.append(adv)
        g = glyf_tbl[name]
        if hasattr(g, "yMax") and g.yMax is not None:
            ymins.append(g.yMin)
            ymaxs.append(g.yMax)

    if not advances or not ymaxs:
        print("  normalize_font: nothing to normalize, skipping")
        return

    from collections import Counter
    adv_counts = Counter(advances)
    # Modal advance: the most common advance width (typically 1024 in Kenney fonts).
    # Using this as scale basis restores the monospaced layout of the pre-1.4.1 fonts
    # where every icon occupied exactly one em (advance == UPM).
    modal_adv = adv_counts.most_common(1)[0][0]
    global_ymin = min(ymins)

    if modal_adv == 0:
        return

    # ── 2. Collect y-statistics for modal-advance glyphs only ────────────────
    # Use these for centering so that outlier-tall icons (e.g. flair_arrows_all,
    # steamdeck trackpad glyphs with yMax=1312) don't shift all typical icons down.
    modal_ymaxs = [
        glyf_tbl[n].yMax for n in icon_names
        if hmtx.metrics.get(n, (0, 0))[0] == modal_adv
        and hasattr(glyf_tbl[n], "yMax") and glyf_tbl[n].yMax is not None
    ]
    modal_ymins = [
        glyf_tbl[n].yMin for n in icon_names
        if hmtx.metrics.get(n, (0, 0))[0] == modal_adv
        and hasattr(glyf_tbl[n], "yMin") and glyf_tbl[n].yMin is not None
    ]
    if not modal_ymaxs:
        return

    # Modal y-statistics (most common yMax/yMin among the typical glyphs)
    typical_ymax = Counter(modal_ymaxs).most_common(1)[0][0]
    typical_ymin = Counter(modal_ymins).most_common(1)[0][0]

    # ── 3. Compute transform ─────────────────────────────────────────────────
    # Uniform scale: modal advance → UPM
    scale = upm / modal_adv

    # Center the typical glyph in the em square
    scaled_typical_center = (typical_ymin + typical_ymax) * scale / 2.0
    translate_y = upm / 2.0 - scaled_typical_center

    # Metrics: ascent from typical top; descent from global bottom (avoids clipping)
    final_ymax = int(round(typical_ymax * scale + translate_y))
    final_ymin = int(round(global_ymin * scale + translate_y))

    print(
        f"  Normalizing glyphs: modal_adv={modal_adv} upm={upm} "
        f"scale={scale:.4f} translate_y={translate_y:+.1f}"
    )
    print(f"  New extents: yMin={final_ymin} yMax={final_ymax}")

    # ── 3. Apply transform to every icon glyph ───────────────────────────────
    new_max_adv = 0
    for name in icon_names:
        g = glyf_tbl[name]

        if g.numberOfContours > 0:
            # Simple glyph – scale + translate coordinates directly
            new_coords = [
                (int(round(x * scale)), int(round(y * scale + translate_y)))
                for x, y in g.coordinates
            ]
            g.coordinates = GlyphCoordinates(new_coords)
            g.recalcBounds(glyf_tbl)

        elif g.numberOfContours == -1:
            # Composite glyph – scale component offsets
            for comp in g.components:
                comp.x = int(round(comp.x * scale))
                comp.y = int(round(comp.y * scale + translate_y))
            g.recalcBounds(glyf_tbl)
        # else: numberOfContours == 0 → empty (ASCII placeholder), skip

        adv, _ = hmtx.metrics.get(name, (0, 0))
        new_adv = int(round(adv * scale))
        hmtx.metrics[name] = (new_adv, 0)
        new_max_adv = max(new_max_adv, new_adv)

    # ── 4. Update font-level metrics ─────────────────────────────────────────
    font["hhea"].ascent = final_ymax
    font["hhea"].descent = final_ymin
    font["hhea"].lineGap = 0
    font["hhea"].advanceWidthMax = new_max_adv

    font["OS/2"].sTypoAscender = final_ymax
    font["OS/2"].sTypoDescender = final_ymin
    font["OS/2"].sTypoLineGap = 0
    font["OS/2"].usWinAscent = final_ymax
    font["OS/2"].usWinDescent = abs(final_ymin)

    font["head"].yMin = final_ymin
    font["head"].yMax = final_ymax


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

    print("\nNormalizing glyph metrics ...")
    normalize_font(merged)

    print("\nAdding ligatures ...")
    add_ligatures(merged, cmap_map)

    OUTPUT_TTF.parent.mkdir(parents=True, exist_ok=True)
    merged.save(str(OUTPUT_TTF))
    print(f"\nSaved → {OUTPUT_TTF}")

    save_version(latest_version)
    print("Done.")


if __name__ == "__main__":
    main()
