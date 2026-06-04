#!/usr/bin/env python3
"""Render the current compiler agent graph as Mermaid, DOT, and Markdown."""

from __future__ import annotations

import argparse
import html
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_graph.compiler import compile_graph_edge_specs, compile_graph_node_specs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="docs", help="Directory for rendered graph artifacts")
    parser.add_argument("--basename", default="agent_graph", help="Output filename prefix")
    parser.add_argument("--render-svg", action="store_true", help="Render DOT to SVG when Graphviz dot is installed")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    nodes = list(compile_graph_node_specs())
    edges = list(compile_graph_edge_specs())

    mmd_path = output_dir / f"{args.basename}.mmd"
    dot_path = output_dir / f"{args.basename}.dot"
    md_path = output_dir / f"{args.basename}.md"
    svg_path = output_dir / f"{args.basename}.svg"

    mermaid = render_mermaid(nodes, edges)
    dot = render_dot(nodes, edges)
    mmd_path.write_text(mermaid, encoding="utf-8")
    dot_path.write_text(dot, encoding="utf-8")
    md_path.write_text(render_markdown(nodes, edges, mermaid), encoding="utf-8")

    svg_status = "skipped"
    if args.render_svg:
        dot_bin = shutil.which("dot")
        if dot_bin:
            subprocess.run([dot_bin, "-Tsvg", str(dot_path), "-o", str(svg_path)], check=True)
            svg_status = str(svg_path)
        else:
            svg_status = "Graphviz dot not found"

    print(f"markdown={md_path}")
    print(f"mermaid={mmd_path}")
    print(f"dot={dot_path}")
    print(f"svg={svg_status}")
    return 0


def render_mermaid(nodes, edges) -> str:
    entry = nodes[0].name if nodes else "START"
    lines = [
        "flowchart TD",
        '  START(["START"]):::entry',
        '  END(["END"]):::terminal',
    ]
    for node in nodes:
        class_name = mermaid_class(node)
        label = mermaid_label(
            [
                node.label,
                f"LLM: {node.uses_llm}",
                f"Tools: {', '.join(node.uses_tools) or 'none'}",
                f"Decision: {node.decided_by}",
            ]
        )
        if node.decided_by == "conditional_logic":
            lines.append(f'  {node.name}{{"{label}"}}:::{class_name}')
        else:
            lines.append(f'  {node.name}["{label}"]:::{class_name}')
    lines.append(f"  START --> {entry}")
    for edge in edges:
        style = "-.->" if edge.edge_type == "conditional" else "-->"
        label = edge.label
        if edge.condition:
            label = f"{label}; {edge.condition}" if label else edge.condition
        target = "END" if edge.target == "END" else edge.target
        if label:
            lines.append(f'  {edge.source} {style}|"{escape_mermaid(label)}"| {target}')
        else:
            lines.append(f"  {edge.source} {style} {target}")
    lines.extend(
        [
            "  classDef entry fill:#eef2ff,stroke:#4f46e5,color:#111827",
            "  classDef terminal fill:#f3f4f6,stroke:#374151,color:#111827",
            "  classDef logic fill:#ecfdf5,stroke:#047857,color:#064e3b",
            "  classDef llm fill:#fef3c7,stroke:#b45309,color:#78350f",
            "  classDef tool fill:#e0f2fe,stroke:#0369a1,color:#0c4a6e",
            "  classDef decision fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d",
            "  classDef human fill:#f5f3ff,stroke:#7c3aed,color:#4c1d95",
        ]
    )
    return "\n".join(lines) + "\n"


def render_dot(nodes, edges) -> str:
    entry = nodes[0].name if nodes else "START"
    lines = [
        "digraph compile_agent_graph {",
        "  rankdir=LR;",
        '  graph [fontname="Arial"];',
        '  node [shape=box, style="rounded,filled", fontname="Arial", fontsize=10];',
        '  edge [fontname="Arial", fontsize=9];',
        '  START [shape=oval, fillcolor="#eef2ff", color="#4f46e5"];',
        '  END [shape=oval, fillcolor="#f3f4f6", color="#374151"];',
    ]
    for node in nodes:
        fill, color, shape = dot_style(node)
        label = "\\n".join(
            [
                node.label,
                f"LLM: {node.uses_llm}",
                f"Tools: {', '.join(node.uses_tools) or 'none'}",
                f"Decision: {node.decided_by}",
            ]
        )
        lines.append(f'  {node.name} [label="{escape_dot(label)}", shape={shape}, fillcolor="{fill}", color="{color}"];')
    lines.append(f"  START -> {entry};")
    for edge in edges:
        target = "END" if edge.target == "END" else edge.target
        attrs = []
        label = edge.label
        if edge.condition:
            label = f"{label}; {edge.condition}" if label else edge.condition
        if label:
            attrs.append(f'label="{escape_dot(label)}"')
        if edge.edge_type == "conditional":
            attrs.append('style="dashed"')
            attrs.append('color="#b91c1c"')
        attr_text = f" [{', '.join(attrs)}]" if attrs else ""
        lines.append(f"  {edge.source} -> {target}{attr_text};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_markdown(nodes, edges, mermaid: str) -> str:
    lines = [
        "# Agent Graph State Diagram",
        "",
        "Generated from `agent_graph.compiler` metadata. Regenerate this file after changing the agent graph:",
        "",
        "```bash",
        ".venv/bin/python scripts/render_agent_graph.py --render-svg",
        "```",
        "",
        "```mermaid",
        mermaid.rstrip(),
        "```",
        "",
        "## Node Semantics",
        "",
        "| Node | Type | LLM | Tools | Decision | State Outputs |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for node in nodes:
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{node.name}`",
                    node.node_type,
                    node.uses_llm,
                    ", ".join(node.uses_tools) or "none",
                    node.decided_by,
                    ", ".join(f"`{item}`" for item in node.state_outputs),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Transitions",
            "",
            "| From | To | Type | Decided By | Condition / Label |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for edge in edges:
        target = "END" if edge.target == "END" else f"`{edge.target}`"
        condition = edge.condition or edge.label
        if edge.condition and edge.label:
            condition = f"{edge.label}; {edge.condition}"
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{edge.source}`",
                    target,
                    edge.edge_type,
                    edge.decided_by,
                    f"`{condition}`" if condition else "",
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def mermaid_class(node) -> str:
    if node.node_type == "human_gate":
        return "human"
    if node.node_type == "logic_pipeline":
        return "logic"
    if node.node_type == "gate_loop" or node.decided_by == "conditional_logic":
        return "decision"
    if "vision_mcp_optional" in node.uses_tools:
        return "tool"
    if node.uses_llm != "no":
        return "llm"
    return "logic"


def dot_style(node) -> tuple[str, str, str]:
    class_name = mermaid_class(node)
    if class_name == "human":
        return "#f5f3ff", "#7c3aed", "box"
    if class_name == "decision":
        return "#fee2e2", "#b91c1c", "diamond"
    if class_name == "tool":
        return "#e0f2fe", "#0369a1", "box"
    if class_name == "llm":
        return "#fef3c7", "#b45309", "box"
    return "#ecfdf5", "#047857", "box"


def mermaid_label(lines: list[str]) -> str:
    return "<br/>".join(escape_mermaid(line) for line in lines)


def escape_mermaid(value: str) -> str:
    return html.escape(value, quote=True).replace("|", "&#124;")


def escape_dot(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


if __name__ == "__main__":
    raise SystemExit(main())
