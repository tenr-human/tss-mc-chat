import argparse
import csv
import html
from pathlib import Path


COLORS = {"baseline": "#4c78a8", "sdn": "#f58518"}
MODE_LABELS = {"baseline": "Baseline", "sdn": "OS-Ken SDN"}
MODES = ("baseline", "sdn")


def read_summary(path, metric):
    grouped = {}
    with path.open(newline="") as source:
        reader = csv.DictReader(source)
        required = {"mode", "clients", "flood", metric}
        missing = required.difference(reader.fieldnames or ())
        if missing:
            raise ValueError(
                str(path) + " is missing columns: " + ", ".join(sorted(missing))
            )
        for row in reader:
            mode = row["mode"]
            if mode not in MODES:
                continue
            key = (int(row["clients"]), row["flood"].lower() == "true")
            runs = int(row.get("runs") or 1)
            total, count = grouped.get((key, mode), (0.0, 0))
            grouped[(key, mode)] = (total + float(row[metric]) * runs, count + runs)

    return {
        (key, mode): total / count
        for (key, mode), (total, count) in grouped.items()
        if count
    }


def make_chart(data, output_path, title, y_label, value_format):
    cases = sorted({key for key, _ in data}, key=lambda item: (item[0], item[1]))
    if not cases:
        raise ValueError("No baseline or SDN results found for " + title)
    if any((case, mode) not in data for case in cases for mode in MODES):
        raise ValueError("Each test case must have both baseline and SDN results")

    width, height = max(760, len(cases) * 130), 480
    left, right, top, bottom = 82, 28, 62, 94
    plot_width = width - left - right
    plot_height = height - top - bottom
    max_value = max(data.values())
    scale_max = max_value * 1.15 if max_value else 1.0
    group_width = plot_width / len(cases)
    bar_width = min(34, group_width * 0.28)
    gap = max(5, bar_width * 0.12)
    baseline_y = top + plot_height
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Arial,sans-serif;fill:#222}.grid{stroke:#ddd;stroke-width:1}.axis{stroke:#555;stroke-width:1}.bar{opacity:.9}</style>",
        f'<text x="{width / 2}" y="30" text-anchor="middle" font-size="20">{html.escape(title)}</text>',
    ]

    for tick in range(6):
        value = scale_max * tick / 5
        y = baseline_y - plot_height * tick / 5
        lines.append(
            f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}"/>'
        )
        lines.append(
            f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" font-size="12">{value_format(value)}</text>'
        )

    lines.extend(
        [
            f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{baseline_y}"/>',
            f'<line class="axis" x1="{left}" y1="{baseline_y}" x2="{width - right}" y2="{baseline_y}"/>',
            f'<text x="20" y="{top + plot_height / 2}" text-anchor="middle" font-size="13" transform="rotate(-90 20 {top + plot_height / 2})">{html.escape(y_label)}</text>',
        ]
    )

    for index, case in enumerate(cases):
        center = left + group_width * (index + 0.5)
        start = center - bar_width - gap / 2
        for mode_index, mode in enumerate(MODES):
            value = data[(case, mode)]
            bar_height = value / scale_max * plot_height
            x = start + mode_index * (bar_width + gap)
            y = baseline_y - bar_height
            lines.append(
                f'<rect class="bar" x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="{COLORS[mode]}"/>'
            )
            lines.append(
                f'<text x="{x + bar_width / 2:.1f}" y="{y - 5:.1f}" text-anchor="middle" font-size="11">{value_format(value)}</text>'
            )
        flood_label = "With flood" if case[1] else "No flood"
        lines.append(
            f'<text x="{center:.1f}" y="{baseline_y + 22}" text-anchor="middle" font-size="12">{case[0]} clients</text>'
        )
        lines.append(
            f'<text x="{center:.1f}" y="{baseline_y + 40}" text-anchor="middle" font-size="12">{flood_label}</text>'
        )

    legend_y = height - 23
    for index, mode in enumerate(MODES):
        x = width / 2 - 78 + index * 110
        lines.append(
            f'<rect x="{x:.1f}" y="{legend_y - 12}" width="14" height="14" fill="{COLORS[mode]}"/>'
        )
        lines.append(
            f'<text x="{x + 20:.1f}" y="{legend_y}" font-size="12">{MODE_LABELS[mode]}</text>'
        )

    lines.append("</svg>")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Created", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Create graphs from the network measurement CSV files"
    )
    parser.add_argument(
        "--results",
        default="results.csv",
        help="measurement CSV path (default: results.csv)",
    )
    parser.add_argument(
        "--output-dir",
        help="graph directory (default: <results name>_graphs)",
    )
    args = parser.parse_args()

    results_path = Path(args.results)
    base = results_path.with_suffix("")
    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else base.parent / (base.name + "_graphs")
    )
    latency_path = base.parent / (base.name + "_summary.csv")
    latency_data = read_summary(latency_path, "average_ms")
    make_chart(
        latency_data,
        output_dir / "chat_latency.svg",
        "Average chat latency",
        "Latency (ms)",
        lambda value: f"{value:.0f}",
    )


if __name__ == "__main__":
    main()
