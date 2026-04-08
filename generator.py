#!/usr/bin/env python3
"""
Generate colored 3MF filament tags for BambuStudio.

Pipeline:
  1. Read filament data from CSV
  2. For each filament, call OpenSCAD to render base and frame STLs (with text)
  3. Pack meshes into 3MF with BambuStudio-compatible color assignments

The 3MF uses <m:colorgroup> / <m:color> from the 3MF Materials Extension,
which is what BambuStudio actually parses (it ignores <basematerials>).

Modes:
  Individual (default):  one 3MF per tag, organized by collection
  Plate (--plate):       one 3MF per category (e.g. "PLA Basic"), all tags
                         arranged on a grid that fits the printer bed
  Codes (--codes):       pick specific tags by color code, single plate output

Usage:
    python generate_tags.py                                    # individual tags
    python generate_tags.py --plate                            # one 3MF per category
    python generate_tags.py --plate --printer a1mini           # plates, 180x180 bed
    python generate_tags.py --codes 10100,10201,33102          # pick by code
    python generate_tags.py --codes 10100,10201 --printer h2d  # pick + printer
    python generate_tags.py --csv ../PLA.csv --plate --limit 5
"""

import argparse
import csv
import logging
import struct
import subprocess
import sys
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

# Defaults
DEFAULT_CSV = SCRIPT_DIR / "filament_data_combined.csv"
DEFAULT_SCAD = SCRIPT_DIR / "templates" / "tag_template.scad"
DEFAULT_OUTPUT = SCRIPT_DIR / "output"
OPENSCAD_BIN = "openscad"

# Tag physical size (from STL bounding box) + spacing
TAG_W = 66.0   # mm, with margin
TAG_H = 15.0   # mm, with margin

# Printer bed sizes (mm)
PRINTERS = {
    "a1mini": (180, 180),
    "a1":     (256, 256),
    "x1c":    (256, 256),
    "x1":     (256, 256),
    "p1s":    (256, 256),
    "p1p":    (256, 256),
    "p2s":    (256, 256),
    "h2c":    (300, 320),  # dual-nozzle overlap area (full bed 330x320)
    "h2d":    (300, 320),  # dual-nozzle overlap area (full bed 350x320)
    "h2dpro": (300, 320),  # dual-nozzle overlap area (full bed 350x320)
    "h2s":    (340, 320),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)


# ── STL loading ──────────────────────────────────────────────────────────────

def load_stl(path: Path):
    """Return (vertices, faces) with deduplicated vertices. Handles binary and ASCII STL."""
    data = path.read_bytes()
    is_ascii = data[:5] == b"solid" and b"\n" in data[:80]

    raw_triangles = []  # list of ((x,y,z), (x,y,z), (x,y,z))

    if is_ascii:
        import re
        text = data.decode("ascii", errors="ignore")
        verts = re.findall(
            r"vertex\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s+([\d.eE+-]+)", text
        )
        for i in range(0, len(verts), 3):
            raw_triangles.append(tuple(
                (float(verts[i+j][0]), float(verts[i+j][1]), float(verts[i+j][2]))
                for j in range(3)
            ))
    else:
        num_triangles = struct.unpack_from("<I", data, 80)[0]
        offset = 84
        for _ in range(num_triangles):
            tri = []
            for j in range(3):
                tri.append(struct.unpack_from("<3f", data, offset + 12 + j * 12))
            raw_triangles.append(tuple(tri))
            offset += 50

    # Deduplicate vertices
    vertex_map = {}
    vertices = []
    faces = []
    for tri in raw_triangles:
        face_indices = []
        for v in tri:
            if v not in vertex_map:
                vertex_map[v] = len(vertices)
                vertices.append(v)
            face_indices.append(vertex_map[v])
        faces.append(tuple(face_indices))

    return vertices, faces


def stl_bounds(vertices):
    """Return (min_x, min_y, min_z, max_x, max_y, max_z)."""
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    return min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)


# ── 3MF XML building blocks ─────────────────────────────────────────────────

_CONTENT_TYPES = """\
<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
</Types>"""

_RELS = """\
<?xml version="1.0" encoding="utf-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
                Target="/3D/3dmodel.model" Id="rel0"/>
</Relationships>"""

_MODEL_RELS = """\
<?xml version="1.0" encoding="utf-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Type="" Target="/Metadata/model_settings.config" Id="rel1"/>
</Relationships>"""


def _mesh_xml(obj_id, name, color_group_id, vertices, faces):
    """Build an <object> element with mesh data and color reference."""
    lines = [
        f'    <object id="{obj_id}" name="{name}" type="model"'
        f' pid="{color_group_id}" pindex="0">',
        "      <mesh>",
        "        <vertices>",
    ]
    for v in vertices:
        lines.append(f'          <vertex x="{v[0]}" y="{v[1]}" z="{v[2]}"/>')
    lines.append("        </vertices>")
    lines.append("        <triangles>")
    for f in faces:
        lines.append(f'          <triangle v1="{f[0]}" v2="{f[1]}" v3="{f[2]}"/>')
    lines.append("        </triangles>")
    lines.append("      </mesh>")
    lines.append("    </object>")
    return "\n".join(lines)


def _write_3mf(output_path, model_xml, model_settings=None):
    """Write the 3MF ZIP archive."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("3D/3dmodel.model", model_xml)
        if model_settings:
            zf.writestr("3D/_rels/3dmodel.model.rels", _MODEL_RELS)
            zf.writestr("Metadata/model_settings.config", model_settings)


# ── Single-tag 3MF ──────────────────────────────────────────────────────────

def build_single_3mf(base_stl, frame_stl, output_3mf):
    """Create a BambuStudio-compatible colored 3MF from two STL files."""
    base_verts, base_faces = load_stl(base_stl)
    frame_verts, frame_faces = load_stl(frame_stl)

    model_xml = "\n".join([
        '<?xml version="1.0" encoding="utf-8"?>',
        '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"',
        '       unit="millimeter" xml:lang="en-US"',
        '       xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02">',
        "  <resources>",
        '    <m:colorgroup id="2">',
        '      <m:color color="#FFFFFFFF"/>',
        "    </m:colorgroup>",
        '    <m:colorgroup id="4">',
        '      <m:color color="#000000FF"/>',
        "    </m:colorgroup>",
        _mesh_xml(3, "Base", 2, base_verts, base_faces),
        _mesh_xml(5, "Frame", 4, frame_verts, frame_faces),
        '    <object id="1" type="model">',
        "      <components>",
        '        <component objectid="3"/>',
        '        <component objectid="5"/>',
        "      </components>",
        "    </object>",
        "  </resources>",
        "  <build>",
        '    <item objectid="1"/>',
        "  </build>",
        "</model>",
    ])

    model_settings = "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<config>",
        '  <object id="1">',
        '    <part id="3" subtype="normal_part">',
        '      <metadata key="name" value="Base"/>',
        "    </part>",
        '    <part id="5" subtype="normal_part">',
        '      <metadata key="name" value="Frame"/>',
        "    </part>",
        "  </object>",
        "</config>",
    ])

    _write_3mf(output_3mf, model_xml, model_settings)


# ── Plate 3MF (multiple tags on one build plate) ────────────────────────────

def build_plate_3mf(tag_stl_pairs, output_3mf, bed_w, bed_h):
    """Create a 3MF with multiple tags arranged in a grid on the plate.

    tag_stl_pairs: list of (label, display_name, base_stl_path, frame_stl_path)
    """
    # Load all meshes and compute the bounding box of a single tag
    # to center the mesh at origin for clean transforms
    tags = []
    for label, display_name, base_path, frame_path in tag_stl_pairs:
        bv, bf = load_stl(base_path)
        fv, ff = load_stl(frame_path)
        # Combined bounds of both meshes
        all_verts = bv + fv
        mn_x, mn_y, mn_z, mx_x, mx_y, mx_z = stl_bounds(all_verts)
        tags.append((label, display_name, bv, bf, fv, ff, mn_x, mn_y, mx_x, mx_y))

    if not tags:
        return

    # Compute grid layout
    # Use first tag's bounds as reference for cell size
    _, _, _, _, _, _, ref_mnx, ref_mny, ref_mxx, ref_mxy = tags[0]
    tag_w = ref_mxx - ref_mnx
    tag_h = ref_mxy - ref_mny
    spacing_x = 3.0  # mm gap between tags
    spacing_y = 3.0
    cell_w = tag_w + spacing_x
    cell_h = tag_h + spacing_y

    cols = max(1, int(bed_w / cell_w))
    rows = max(1, int(bed_h / cell_h))
    tags_per_plate = cols * rows

    if len(tags) > tags_per_plate:
        log.warning("Too many tags (%d) for one plate (%d×%d = %d). "
                    "Only first %d will be placed.",
                    len(tags), cols, rows, tags_per_plate, tags_per_plate)
        tags = tags[:tags_per_plate]

    log.info("Plate layout: %d×%d grid, %d tags, bed %d×%d mm",
             cols, rows, len(tags), bed_w, bed_h)

    # Assign IDs: colorgroups 2 (white) and 4 (black), matching single-tag format.
    # Per tag: base_obj_id, frame_obj_id, container_obj_id
    # Start object IDs at 5 (1-4 reserved for colorgroups)
    next_id = 5
    tag_ids = []  # (container_id, base_id, frame_id) per tag
    for _ in tags:
        base_id = next_id
        frame_id = next_id + 1
        container_id = next_id + 2
        tag_ids.append((container_id, base_id, frame_id))
        next_id += 3

    # Build model XML
    resource_lines = []
    for i, (tag_data, ids) in enumerate(zip(tags, tag_ids)):
        label, display_name, bv, bf, fv, ff, mn_x, mn_y, mx_x, mx_y = tag_data
        container_id, base_id, frame_id = ids
        resource_lines.append(_mesh_xml(base_id, "Base", 2, bv, bf))
        resource_lines.append(_mesh_xml(frame_id, "Frame", 4, fv, ff))
        dn = display_name.replace("&", "&amp;").replace('"', "&quot;")
        resource_lines.append(
            f'    <object id="{container_id}" name="{dn}" type="model">\n'
            f"      <components>\n"
            f'        <component objectid="{base_id}"/>\n'
            f'        <component objectid="{frame_id}"/>\n'
            f"      </components>\n"
            f"    </object>"
        )

    # Build items with grid transforms
    # Center the grid on the bed; offset meshes so their origin is at (0,0)
    ref_mnx, ref_mny = tags[0][6], tags[0][7]
    grid_total_w = cols * cell_w - spacing_x
    grid_total_h = rows * cell_h - spacing_y
    origin_x = (bed_w - grid_total_w) / 2 - ref_mnx
    origin_y = (bed_h - grid_total_h) / 2 - ref_mny

    build_lines = []
    for i, (_, ids) in enumerate(zip(tags, tag_ids)):
        container_id = ids[0]
        col = i % cols
        row = i // cols
        tx = origin_x + col * cell_w
        ty = origin_y + row * cell_h
        build_lines.append(
            f'    <item objectid="{container_id}" '
            f'transform="1 0 0 0 1 0 0 0 1 {tx:.2f} {ty:.2f} 0"/>'
        )

    model_xml = "\n".join([
        '<?xml version="1.0" encoding="utf-8"?>',
        '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"',
        '       unit="millimeter" xml:lang="en-US"',
        '       xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02">',
        "  <resources>",
        '    <m:colorgroup id="2">',
        '      <m:color color="#FFFFFFFF"/>',
        "    </m:colorgroup>",
        '    <m:colorgroup id="4">',
        '      <m:color color="#000000FF"/>',
        "    </m:colorgroup>",
        *resource_lines,
        "  </resources>",
        "  <build>",
        *build_lines,
        "  </build>",
        "</model>",
    ])

    # model_settings.config with part names (Base/Frame) for each object
    config_lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>"]
    for tag_data, ids in zip(tags, tag_ids):
        display_name = tag_data[1]
        dn = display_name.replace("&", "&amp;").replace('"', "&quot;")
        container_id, base_id, frame_id = ids
        config_lines.extend([
            f'  <object id="{container_id}">',
            f'    <metadata key="name" value="{dn}"/>',
            f'    <part id="{base_id}" subtype="normal_part">',
            f'      <metadata key="name" value="Base"/>',
            f"    </part>",
            f'    <part id="{frame_id}" subtype="normal_part">',
            f'      <metadata key="name" value="Frame"/>',
            f"    </part>",
            f"  </object>",
        ])
    config_lines.append("</config>")

    _write_3mf(output_3mf, model_xml, "\n".join(config_lines))


# ── OpenSCAD rendering ───────────────────────────────────────────────────────

def render_stl(scad_path, output_stl, extra_args):
    """Call OpenSCAD to render one STL."""
    cmd = [OPENSCAD_BIN, "-o", str(output_stl), "--export-format=binstl"] + extra_args + [str(scad_path)]
    log.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("OpenSCAD failed:\n%s", result.stderr)
        return False
    return True


def sanitize(name):
    """Remove only filesystem-unsafe characters, keep spaces and hyphens."""
    return "".join(c for c in name if c not in '/\\:*?"<>|').strip()


def render_tag_stls(row, scad_path, tmp):
    """Render base and frame STLs for a CSV row. Returns (label, base_path, frame_path) or None."""
    collection = row["Collection"]
    category = row["Category"]
    color_name = row["Name"]
    color_code = row["Code"]

    tag_name = f"{sanitize(category)} - {sanitize(color_name)} ({color_code})"
    display_name = tag_name
    base_stl = tmp / f"{tag_name}_base.stl"
    frame_stl = tmp / f"{tag_name}_frame.stl"

    if collection.lower() == "support":
        label_name, label_type = category, color_name
    else:
        label_name, label_type = color_name, category

    common_args = [
        "-D", f'color_name="{label_name}"',
        "-D", f'filament_type="{label_type}"',
        "-D", f'color_code="{color_code}"',
    ]

    log.info("Rendering %s %s (%s)", category, color_name, color_code)

    if not render_stl(scad_path, base_stl, ["-D", 'export_part="base"'] + common_args):
        return None
    if not render_stl(scad_path, frame_stl, ["-D", 'export_part="frame"'] + common_args):
        return None

    return tag_name, display_name, base_stl, frame_stl


# ── Main pipeline ────────────────────────────────────────────────────────────

def parse_bed_size(printer_str):
    """Parse printer name or WxH string into (width, height)."""
    key = printer_str.lower().replace(" ", "").replace("-", "")
    if key in PRINTERS:
        return PRINTERS[key]
    if "x" in printer_str.lower():
        parts = printer_str.lower().split("x")
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            pass
    log.error("Unknown printer '%s'. Use one of: %s or WxH (e.g. 220x220)",
              printer_str, ", ".join(PRINTERS.keys()))
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Generate colored 3MF filament tags")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Input CSV file")
    parser.add_argument("--scad", type=Path, default=DEFAULT_SCAD, help="OpenSCAD template")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output directory")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of tags (0 = all)")
    parser.add_argument("--plate", action="store_true",
                        help="One 3MF per category, tags arranged on a grid")
    parser.add_argument("--codes", type=str,
                        help="Comma-separated color codes to generate a single plate "
                             "(e.g. 10100,10201,33102)")
    parser.add_argument("--printer", type=str, default="x1c",
                        help="Printer model or WxH bed size (default: x1c = 256x256)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.csv.exists():
        log.error("CSV not found: %s", args.csv)
        sys.exit(1)
    if not args.scad.exists():
        log.error("SCAD template not found: %s", args.scad)
        sys.exit(1)

    bed_w, bed_h = parse_bed_size(args.printer)

    # Verify OpenSCAD is available
    try:
        subprocess.run([OPENSCAD_BIN, "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        log.error("OpenSCAD not found. Install it or set OPENSCAD_BIN.")
        sys.exit(1)

    # Read CSV
    with open(args.csv, newline="") as f:
        all_rows = list(csv.DictReader(f))

    # Build code lookup for --codes
    code_lookup = {row["Code"]: row for row in all_rows}

    # Filter rows
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",")]
        rows = []
        for code in codes:
            if code in code_lookup:
                rows.append(code_lookup[code])
            else:
                log.warning("Code %s not found in CSV, skipping", code)
        if not rows:
            log.error("No matching codes found")
            sys.exit(1)
    else:
        rows = all_rows

    if args.limit:
        rows = rows[:args.limit]

    log.info("Processing %d tags from %s", len(rows), args.csv)

    with tempfile.TemporaryDirectory(prefix="bambu_tags_") as tmp:
        tmp_path = Path(tmp)

        if args.codes:
            # Single plate with all requested codes
            tag_stls = []
            ok, fail = 0, 0
            for row in rows:
                result = render_tag_stls(row, args.scad, tmp_path)
                if result:
                    tag_stls.append(result)
                else:
                    fail += 1

            if tag_stls:
                out_path = args.output / "custom_plate.3mf"
                build_plate_3mf(tag_stls, out_path, bed_w, bed_h)
                log.info("Created %s (%d tags)", out_path, len(tag_stls))
                ok = len(tag_stls)

                for _, _, base_p, frame_p in tag_stls:
                    base_p.unlink(missing_ok=True)
                    frame_p.unlink(missing_ok=True)

            log.info("Done (custom): %d succeeded, %d failed", ok, fail)

        elif args.plate:
            # Group rows by Category
            groups = defaultdict(list)
            for row in rows:
                groups[row["Category"]].append(row)

            ok, fail = 0, 0
            for category, group_rows in groups.items():
                log.info("=== Plate: %s (%d tags) ===", category, len(group_rows))

                # Render all tag STLs for this category
                tag_stls = []
                for row in group_rows:
                    result = render_tag_stls(row, args.scad, tmp_path)
                    if result:
                        tag_stls.append(result)
                    else:
                        fail += 1

                if not tag_stls:
                    continue

                # Split into plates if needed
                cols = max(1, int(bed_w / TAG_W))
                plate_rows = max(1, int(bed_h / TAG_H))
                tags_per_plate = cols * plate_rows
                plate_num = 0

                for start in range(0, len(tag_stls), tags_per_plate):
                    plate_num += 1
                    batch = tag_stls[start:start + tags_per_plate]
                    cat_clean = sanitize(category)

                    if plate_num == 1 and start + tags_per_plate >= len(tag_stls):
                        # Single plate — no suffix needed
                        out_path = args.output / "plates" / f"{cat_clean}.3mf"
                    else:
                        out_path = args.output / "plates" / f"{cat_clean}_plate{plate_num}.3mf"

                    build_plate_3mf(batch, out_path, bed_w, bed_h)
                    log.info("Created plate: %s (%d tags)", out_path, len(batch))
                    ok += len(batch)

                # Cleanup temp STLs for this group
                for _, _, base_p, frame_p in tag_stls:
                    base_p.unlink(missing_ok=True)
                    frame_p.unlink(missing_ok=True)

            log.info("Done (plates): %d succeeded, %d failed", ok, fail)

        else:
            # Individual mode
            ok, fail = 0, 0
            for row in rows:
                result = render_tag_stls(row, args.scad, tmp_path)
                if result:
                    tag_name, _, base_stl, frame_stl = result
                    collection = sanitize(row["Collection"])
                    out_path = args.output / collection / f"{tag_name}.3mf"
                    build_single_3mf(base_stl, frame_stl, out_path)
                    base_stl.unlink(missing_ok=True)
                    frame_stl.unlink(missing_ok=True)
                    log.info("Created %s", out_path)
                    ok += 1
                else:
                    fail += 1

            log.info("Done: %d succeeded, %d failed", ok, fail)

    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
