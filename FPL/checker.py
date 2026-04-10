#!/usr/bin/env python3
"""
mark_close_holes.py

Reads a KiCad .kicad_pcb file, finds any drill holes (pads with drills,
mounting holes, via holes) whose edges are closer than MIN_CLEARANCE mm
to each other, and adds a silkscreen circle around each offending hole
on the F.Silkscreen layer.

Usage:
    python mark_close_holes.py input.kicad_pcb [output.kicad_pcb] [--clearance 0.4]
"""

import re
import sys
import math
import argparse
from pathlib import Path


# ---------------------------------------------------------------------------
# S-expression parser (lightweight, no external deps)
# ---------------------------------------------------------------------------

def parse_sexp(text: str):
    """Parse an S-expression string into nested Python lists."""
    tokens = tokenize(text)
    result, _ = read_tokens(tokens, 0)
    return result


def tokenize(text: str):
    token_re = re.compile(
        r'"(?:[^"\\]|\\.)*"'   # quoted string
        r'|[()]'               # parentheses
        r'|[^\s()"]+'          # atom
    )
    return token_re.findall(text)


def read_tokens(tokens, pos):
    token = tokens[pos]
    if token == '(':
        lst = []
        pos += 1
        while tokens[pos] != ')':
            item, pos = read_tokens(tokens, pos)
            lst.append(item)
        return lst, pos + 1  # skip ')'
    elif token == ')':
        raise SyntaxError("Unexpected ')'")
    else:
        # strip quotes from strings
        if token.startswith('"') and token.endswith('"'):
            token = token[1:-1]
        return token, pos + 1


def sexp_to_str(node, indent=0) -> str:
    """Serialize a parsed S-expression back to text."""
    if isinstance(node, str):
        # re-quote if contains spaces / special chars
        if needs_quoting(node):
            escaped = node.replace('\\', '\\\\').replace('"', '\\"')
            return f'"{escaped}"'
        return node
    # list node
    if not node:
        return '()'
    inner = node[0]
    single_line_tags = {
        'at', 'size', 'drill', 'layers', 'net', 'tstamp', 'uuid',
        'width', 'angle', 'start', 'end', 'center', 'mid', 'xy',
        'effects', 'font', 'justify',
    }
    tag = node[0] if node else ''
    force_single = isinstance(tag, str) and tag in single_line_tags
    children = [sexp_to_str(child, indent + 2) for child in node]

    # decide layout
    has_list_child = any(isinstance(c, list) for c in node[1:])
    if force_single or not has_list_child:
        return '(' + ' '.join(children) + ')'
    else:
        pad = ' ' * (indent + 2)
        lines = ['(' + children[0]]
        for c in children[1:]:
            lines.append(pad + c)
        lines[-1] += ')'
        return ('\n' + pad).join(lines)


def needs_quoting(s: str) -> bool:
    return bool(re.search(r'[\s()"\\]', s)) or s == ''


# ---------------------------------------------------------------------------
# Hole extraction
# ---------------------------------------------------------------------------

def find_attr(node, key):
    """Return the first child list whose first element == key, or None."""
    for child in node:
        if isinstance(child, list) and child and child[0] == key:
            return child
    return None


def find_all(node, key):
    results = []
    for child in node:
        if isinstance(child, list) and child and child[0] == key:
            results.append(child)
    return results


def extract_holes(pcb):
    """
    Return a list of dicts:
        { 'x': float, 'y': float, 'r': float, 'source': str }
    r = radius of the drill hole (half the drill diameter).
    """
    holes = []

    # --- vias ---
    for via in find_all(pcb, 'via'):
        at = find_attr(via, 'at')
        drill = find_attr(via, 'drill')
        if at and drill:
            x, y = float(at[1]), float(at[2])
            d = float(drill[1])
            holes.append({'x': x, 'y': y, 'r': d / 2, 'source': 'via'})

    # --- pads inside footprints ---
    for fp in find_all(pcb, 'footprint'):
        fp_at = find_attr(fp, 'at')
        fp_x = float(fp_at[1]) if fp_at else 0.0
        fp_y = float(fp_at[2]) if fp_at and len(fp_at) > 2 else 0.0
        fp_angle = float(fp_at[3]) if fp_at and len(fp_at) > 3 else 0.0

        for pad in find_all(fp, 'pad'):
            pad_type = pad[2] if len(pad) > 2 else ''
            if pad_type not in ('thru_hole', 'np_thru_hole'):
                continue
            drill = find_attr(pad, 'drill')
            if not drill:
                continue
            at = find_attr(pad, 'at')
            if not at:
                continue
            lx, ly = float(at[1]), float(at[2]) if len(at) > 2 else 0.0
            # rotate pad position by footprint angle
            if fp_angle:
                rad = math.radians(fp_angle)
                rx = lx * math.cos(rad) - ly * math.sin(rad)
                ry = lx * math.sin(rad) + ly * math.cos(rad)
                lx, ly = rx, ry
            wx, wy = fp_x + lx, fp_y + ly

            # drill node: (drill [oval] diameter [height] ...)
            idx = 1
            if len(drill) > idx and drill[idx] == 'oval':
                idx += 1
            d = float(drill[idx]) if len(drill) > idx else 0.8
            holes.append({'x': wx, 'y': wy, 'r': d / 2, 'source': 'pad'})

    return holes


# ---------------------------------------------------------------------------
# Proximity check
# ---------------------------------------------------------------------------

def find_close_pairs(holes, min_clearance: float):
    """
    Return the set of hole indices that are too close to at least one other hole.
    "Too close" means the gap between hole edges < min_clearance.
    Edge-to-edge distance = center distance - r1 - r2.
    """
    flagged = set()
    n = len(holes)
    for i in range(n):
        for j in range(i + 1, n):
            dx = holes[i]['x'] - holes[j]['x']
            dy = holes[i]['y'] - holes[j]['y']
            center_dist = math.hypot(dx, dy)
            edge_gap = center_dist - holes[i]['r'] - holes[j]['r']
            if edge_gap < min_clearance:
                flagged.add(i)
                flagged.add(j)
    return flagged


# ---------------------------------------------------------------------------
# Silkscreen circle generation
# ---------------------------------------------------------------------------

MARKER_LAYER = 'F.Silkscreen'
MARKER_WIDTH = 0.1   # line width in mm
MARKER_MARGIN = 0.15  # extra margin beyond hole radius


def make_circle_sexp(x: float, y: float, radius: float) -> list:
    """Build an S-expression list for a gr_circle on F.Silkscreen."""
    cx = f'{x:.6f}'
    cy = f'{y:.6f}'
    # KiCad circle: center + any point on circumference (use top of circle)
    ex = f'{x:.6f}'
    ey = f'{y - radius:.6f}'
    w = f'{MARKER_WIDTH:.4f}'
    return [
        'gr_circle',
        ['center', cx, cy],
        ['end', ex, ey],
        ['stroke', ['width', w], ['type', 'solid']],
        ['fill', 'none'],
        ['layer', MARKER_LAYER],
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process(input_path: Path, output_path: Path, min_clearance: float):
    print(f"Reading: {input_path}")
    text = input_path.read_text(encoding='utf-8')

    print("Parsing S-expression …")
    pcb = parse_sexp(text)

    print("Extracting holes …")
    holes = extract_holes(pcb)
    print(f"  Found {len(holes)} drill holes")

    print(f"Checking clearance < {min_clearance} mm …")
    flagged = find_close_pairs(holes, min_clearance)
    print(f"  {len(flagged)} holes flagged")

    if not flagged:
        print("No violations found — output file is a copy of input.")
        output_path.write_text(text, encoding='utf-8')
        return

    # Build circle nodes and append to the pcb list
    new_circles = []
    for idx in sorted(flagged):
        h = holes[idx]
        r = h['r'] + MARKER_MARGIN
        circle = make_circle_sexp(h['x'], h['y'], r)
        new_circles.append(circle)
        print(f"  Marking hole at ({h['x']:.3f}, {h['y']:.3f}) "
              f"r={h['r']:.3f} source={h['source']}")

    # Append circles just before the closing paren of the kicad_pcb node
    pcb.extend(new_circles)

    print(f"Writing: {output_path}")
    out_text = sexp_to_str(pcb)
    output_path.write_text(out_text, encoding='utf-8')
    print("Done.")


def main():
    parser = argparse.ArgumentParser(
        description='Mark drill holes closer than a threshold with silkscreen circles.'
    )
    parser.add_argument('input', help='Input .kicad_pcb file')
    parser.add_argument('output', nargs='?', help='Output .kicad_pcb file (default: input_marked.kicad_pcb)')
    parser.add_argument('--clearance', type=float, default=0.4,
                        help='Minimum edge-to-edge clearance in mm (default: 0.4)')
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_stem(input_path.stem + '_marked')

    process(input_path, output_path, args.clearance)


if __name__ == '__main__':
    main()
