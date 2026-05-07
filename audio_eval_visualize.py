"""
音频评测 CSV 可视化工具
======================

功能：
1) 读取 audio_eval 产生的实时 CSV
2) 按评分规则计算每条记录的分项评分（丢包/时延/抖动/乱序/重复）
3) 生成堆叠柱状图展示分项评分，并叠加总分曲线

用法示例：
    python audio_eval_visualize.py \
        --csv eval_net/audio_quality_realtime_20260417_152043.csv
"""

import argparse
import csv
import math
from pathlib import Path
from typing import Any, Dict, List

import matplotlib.pyplot as plt

from audio_eval import AudioQualityEvaluator


def _to_float(value: str, default: float = 0.0) -> float:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        return float(text)
    except Exception:
        return default


def _normalize_row(row: dict) -> dict:
    ts = row.get("时刻") or row.get("timestamp") or ""
    loss_pct = _to_float(row.get("丢包率"), 0.0)
    delay_ms = _to_float(row.get("时延"), 0.0)
    jitter_ms = _to_float(row.get("抖动"), 0.0)
    reorder_pct = _to_float(row.get("乱序"), 0.0)
    duplicate_pct = _to_float(row.get("重复"), 0.0)
    total_score_csv = _to_float(row.get("总分"), 0.0)

    loss_rate = max(0.0, loss_pct / 100.0)
    reorder_rate = max(0.0, reorder_pct / 100.0)
    duplicate_rate = max(0.0, duplicate_pct / 100.0)

    s_loss = AudioQualityEvaluator.score_loss(loss_rate)
    s_delay = AudioQualityEvaluator.score_delay(delay_ms)
    s_jitter = AudioQualityEvaluator.score_jitter(jitter_ms)
    s_reorder = AudioQualityEvaluator.score_reorder(reorder_rate)
    s_duplicate = AudioQualityEvaluator.score_duplicate(duplicate_rate)

    total_score_calc = s_loss + s_delay + s_jitter + s_reorder + s_duplicate
    total_score_show = total_score_csv if total_score_csv > 0 else total_score_calc

    return {
        "time": ts,
        "s_loss": s_loss,
        "s_delay": s_delay,
        "s_jitter": s_jitter,
        "s_reorder": s_reorder,
        "s_duplicate": s_duplicate,
        "total_calc": total_score_calc,
        "total_csv": total_score_csv,
        "total_show": total_score_show,
    }


def load_and_score(csv_path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(_normalize_row(row))
    return rows


def plot_scores(rows: List[Dict[str, Any]], output_path: Path, title: str, show: bool, max_bars: int) -> None:
    if not rows:
        raise ValueError("CSV 中没有可用数据。")

    # 记录太多时自动抽样，避免 x 轴过密
    step = max(1, math.ceil(len(rows) / max_bars))
    rows_draw = rows[::step]

    x = list(range(len(rows_draw)))
    labels = [r["time"] for r in rows_draw]

    s_loss = [r["s_loss"] for r in rows_draw]
    s_delay = [r["s_delay"] for r in rows_draw]
    s_jitter = [r["s_jitter"] for r in rows_draw]
    s_reorder = [r["s_reorder"] for r in rows_draw]
    s_duplicate = [r["s_duplicate"] for r in rows_draw]
    total = [r["total_show"] for r in rows_draw]

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(15, 7), dpi=120)

    bottoms = [0] * len(x)
    stack_items = [
        ("丢包评分", s_loss, "#d1495b"),
        ("时延评分", s_delay, "#edae49"),
        ("抖动评分", s_jitter, "#66a182"),
        ("乱序评分", s_reorder, "#2e86ab"),
        ("重复评分", s_duplicate, "#4f5d75"),
    ]

    for name, values, color in stack_items:
        ax.bar(x, values, bottom=bottoms, label=name, color=color, width=0.8, alpha=0.88)
        bottoms = [b + v for b, v in zip(bottoms, values)]

    ax.plot(x, total, color="#111111", linewidth=1.6, marker="o", markersize=2.5, label="总分")

    if len(x) <= 80:
        for i, y in enumerate(total):
            ax.text(i, y + 1.0, f"{int(round(y))}", ha="center", va="bottom", fontsize=7, color="#222222")

    ax.set_ylim(0, 105)
    ax.set_ylabel("评分")
    subtitle = f"样本数: {len(rows)}"
    if step > 1:
        subtitle += f" | 抽样步长: {step}"
    ax.set_title(f"{title}\n{subtitle}")

    tick_step = max(1, len(x) // 12)
    tick_idx = list(range(0, len(x), tick_step))
    if tick_idx[-1] != len(x) - 1:
        tick_idx.append(len(x) - 1)

    ax.set_xticks(tick_idx)
    ax.set_xticklabels([labels[i] for i in tick_idx], rotation=25, ha="right")

    ax.grid(axis="y", linestyle="--", alpha=0.35)
    ax.legend(loc="upper right", ncol=3)
    fig.tight_layout()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path)

    if show:
        plt.show()
    else:
        plt.close(fig)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="音频评测 CSV 可视化（分项评分堆叠图 + 总分）")
    parser.add_argument("--csv", required=True, help="输入 CSV 路径")
    parser.add_argument("--out", default="", help="输出图片路径，默认与 CSV 同目录")
    parser.add_argument("--title", default="网络音频质量分项评分", help="图表标题")
    parser.add_argument("--max-bars", type=int, default=300, help="最多显示多少根柱子，超出将自动抽样")
    parser.add_argument("--show", action="store_true", help="生成后弹窗显示图像")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV 文件不存在: {csv_path}")

    out_path = Path(args.out) if args.out else csv_path.with_name(csv_path.stem + "_stacked_scores.png")

    rows = load_and_score(csv_path)
    plot_scores(rows, out_path, args.title, args.show, max(20, int(args.max_bars)))

    print(f"图表已生成: {out_path}")
    print(f"有效样本: {len(rows)}")


if __name__ == "__main__":
    main()
