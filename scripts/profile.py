#!/usr/bin/env python3
"""Turn native sampler output into measured-window folded stacks and an SVG flamegraph."""
from __future__ import annotations

import argparse
import html
import json
import re
from collections import Counter
from pathlib import Path


def safe_frame(name: str) -> str:
    # Folded-stack syntax reserves semicolons for stack separators.
    return re.sub(r"[\r\n\t]+", " ", name).replace(";", ":").strip() or "<unknown>"


def load_inputs(prefix: Path):
    samples_path = Path(str(prefix) + ".samples.json")
    child_path = Path(str(prefix) + ".child.json")
    samples_data = json.loads(samples_path.read_text(encoding="utf-8-sig"))
    child_data = json.loads(child_path.read_text(encoding="utf-8-sig"))
    for key in ("start_unix_us", "end_unix_us"):
        if not isinstance(child_data.get(key), int):
            raise ValueError(f"{child_path} has no integer {key}")
    if child_data.get("verified") is not True or samples_data.get("child_exit_code") != 0:
        raise ValueError("Child benchmark did not complete successfully with a verified result")
    start, end = child_data["start_unix_us"], child_data["end_unix_us"]
    if end <= start:
        raise ValueError("Child workload interval is empty or reversed")
    samples = samples_data.get("samples")
    counts = samples_data.get("counts", {})
    if not isinstance(samples, list):
        raise ValueError(f"{samples_path} has no samples array")
    succeeded = counts.get("succeeded")
    failed = counts.get("failed")
    attempts = counts.get("attempts")
    if not all(isinstance(v, int) and v >= 0 for v in (succeeded, failed, attempts)):
        raise ValueError("Sampler counts are missing or invalid")
    if succeeded != len(samples) or attempts != succeeded + failed:
        raise ValueError("Sampler sample and attempt counts are inconsistent")
    if samples_data.get("sampling_mode") != "wall_clock_user_mode_main_thread":
        raise ValueError("Input is not the expected user-mode main-thread sampler output")
    return samples_path, child_path, samples_data, child_data, start, end


def tree_add(root, stack):
    node = root
    node["count"] += 1
    for name in stack:
        node = node["children"].setdefault(name, {"count": 0, "children": {}})
        node["count"] += 1


def color_for(name: str) -> str:
    # Stable warm palette; deterministic across runs and Python hash seeds.
    h = 2166136261
    for byte in name.encode("utf-8", "replace"):
        h = ((h ^ byte) * 16777619) & 0xFFFFFFFF
    hue = h % 36
    light = 82 + ((h >> 8) % 9)
    return f"hsl({hue}, 78%, {light}%)"


def render_svg(root, destination: Path, width=1200, row=22):
    max_depth = 0

    def depth(node, current):
        nonlocal max_depth
        max_depth = max(max_depth, current)
        for child in node["children"].values():
            depth(child, current + 1)

    depth(root, 0)
    height = max(115, (max_depth + 1) * row + 100)
    label_height = 70
    root_count = root["count"]
    output = [
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="Sampled stack flamegraph">',
        '<style>text{font-family:Segoe UI,Arial,sans-serif}.frame{cursor:default}.frame rect{stroke:#fff;stroke-width:.7}.frame:hover rect{stroke:#111;stroke-width:1.5}.label{font-size:11px;fill:#111;stroke:none;pointer-events:none}.toolbar{font-size:14px}</style>',
        f'<text x="8" y="19" class="toolbar">Flamegraph | {root_count} workload-window samples | search highlights matching frames</text>',
        '<foreignObject x="8" y="30" width="280" height="30"><body xmlns="http://www.w3.org/1999/xhtml" style="margin:0"><input id="search" placeholder="Search frames" oninput="searchFrames(this.value)" style="width:260px;padding:4px"/></body></foreignObject>',
        f'<g id="flames" transform="translate(0,{label_height})">',
    ]

    def draw(node, name, x, y, w, depth_index, path):
        if name is not None and w > 0.18:
            rect_y = max_depth * row - depth_index * row
            label = html.escape(name, quote=True)
            title = html.escape(f"{name}\nInclusive samples: {node['count']} ({100*node['count']/root_count:.2f}%)\nStack: {';'.join(path)}", quote=True)
            output.append(f'<g class="frame" data-name="{label.lower()}" data-x="{x:.3f}" data-y="{rect_y}" data-w="{w:.3f}" data-depth="{depth_index}">')
            output.append(f'<title>{title}</title><rect x="{x:.3f}" y="{rect_y}" width="{max(0,w):.3f}" height="{row-1}" rx="2" fill="{color_for(name)}"/>')
            if w >= 30:
                limit = max(2, int((w - 8) / 6.5))
                shown = name if len(name) <= limit else name[:limit-1] + "?"
                output.append(f'<text class="label" x="{x+4:.3f}" y="{rect_y+15}">{html.escape(shown)}</text>')
            output.append('</g>')
        children = sorted(node["children"].items(), key=lambda item: (-item[1]["count"], item[0]))
        cursor = x
        for child_name, child in children:
            child_width = w * child["count"] / node["count"] if node["count"] else 0
            draw(child, child_name, cursor, y, child_width, depth_index + 1, path + [child_name])
            cursor += child_width

    if root_count:
        draw(root, None, 0, 0, width, -1, [])
        output += [
            '</g>',
            f'<text id="sample-label" x="8" y="{height-8}" font-size="11">Samples are weighted by observed workload-window hits</text>',
            '<script><![CDATA[function searchFrames(q){q=q.toLowerCase();document.querySelectorAll(".frame").forEach(function(e){e.style.opacity=(!q||e.dataset.name.indexOf(q)>=0)?"1":".16"})}]]></script>',
        ]
    else:
        output += ['</g>', f'<text x="8" y="{label_height+24}" font-size="15">No valid stack samples fell inside the measured workload interval.</text>']
    output.append('</svg>')
    destination.write_text("\n".join(output) + "\n", encoding="utf-8")


def profile(prefix: Path):
    samples_path, child_path, sampler, child, start, end = load_inputs(prefix)
    folded_counts = Counter()
    inclusive = Counter()
    self_counts = Counter()
    kept = 0
    for sample in sampler["samples"]:
        stamp = sample.get("timestamp_unix_us")
        frames = sample.get("frames")
        if not isinstance(stamp, int) or not isinstance(frames, list) or not all(isinstance(f, str) for f in frames):
            raise ValueError("Sample record has invalid timestamp or frames")
        if not start <= stamp <= end or not frames:
            continue
        # Native samples are leaf-first; folded stacks are root-to-leaf.
        stack = [safe_frame(frame) for frame in reversed(frames)]
        folded_counts[";".join(stack)] += 1
        for frame in set(stack):
            inclusive[frame] += 1
        self_counts[stack[-1]] += 1
        kept += 1

    if kept == 0:
        raise ValueError("No successful stack samples fell inside the benchmark workload window")

    folded_path = Path(str(prefix) + ".folded")
    svg_path = Path(str(prefix) + ".svg")
    top_path = Path(str(prefix) + ".top.txt")
    folded_path.write_text("".join(f"{stack} {count}\n" for stack, count in sorted(folded_counts.items())), encoding="utf-8")
    root = {"count": 0, "children": {}}
    for stack, count in folded_counts.items():
        parts = stack.split(";")
        for _ in range(count):
            tree_add(root, parts)
    render_svg(root, svg_path)
    lines = [
        "Native user-mode stack sampling summary",
        f"Sampling mode: {sampler['sampling_mode']}",
        f"Caveat: {sampler.get('caveat', 'See sampler metadata.')}",
        f"Child workload window (UTC Unix microseconds): {start} .. {end}",
        f"Sampler observation duration: {sampler.get('observed_duration_ms')} ms",
        f"Interval: {sampler.get('interval_ms')} ms",
        f"Attempts / successful / failed: {sampler['counts']['attempts']} / {sampler['counts']['succeeded']} / {sampler['counts']['failed']}",
        f"Samples inside measured workload window: {kept}",
        "",
        "Top functions by inclusive samples (estimated from sampled stacks):",
        "inclusive  self  function",
    ]
    for name, count in sorted(inclusive.items(), key=lambda pair: (-pair[1], pair[0]))[:40]:
        lines.append(f"{count:9d} {self_counts[name]:5d}  {name}")
    top_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Samples in workload window: {kept}")
    print(f"Folded stacks: {folded_path}")
    print(f"Flamegraph SVG: {svg_path}")
    print(f"Top functions: {top_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prefix", type=Path, help="sampler output prefix (without .samples.json)")
    args = parser.parse_args()
    profile(args.prefix)


if __name__ == "__main__":
    main()
