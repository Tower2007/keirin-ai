"""過去ライン逆引き feasibility の軽量調査。

依存は標準ライブラリのみ。入力 CSV は読み取り専用。

主な確認:
- race_results.in_line_jyuni の値分布
- race_lines.narabi_x/narabi_y の実態
- narabi_x の座標ギャップから公式ラインを復元できるか
- 検証レースに対し、結果/出走表だけの簡易ヒューリスティックがどの程度当たるか
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path


KEY = tuple[str, str, str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=r"D:\keirin-ai-data")
    return parser.parse_args()


def key(row: dict[str, str]) -> KEY:
    return (row["race_date"], row["place_code"], row["race_no"])


def to_int(value: str) -> int | None:
    try:
        if value == "" or value is None:
            return None
        return int(value)
    except ValueError:
        return None


def normalize_groups(groups: list[list[int]]) -> tuple[tuple[int, ...], ...]:
    """グループ内順序を保持し、グループ順は先頭車番で安定化して比較する。"""
    cleaned = [tuple(g) for g in groups if g]
    return tuple(sorted(cleaned, key=lambda g: (g[0], len(g), g)))


def line_groups_from_x(rows: list[dict[str, str]]) -> list[list[int]]:
    """narabi_x を横座標とみなし、x のギャップ > 1 でラインを区切る。"""
    items: list[tuple[int, int]] = []
    for r in rows:
        car = to_int(r["car_no"])
        x = to_int(r["narabi_x"])
        if car is not None and x is not None:
            items.append((x, car))
    items.sort()
    groups: list[list[int]] = []
    current: list[int] = []
    prev_x: int | None = None
    for x, car in items:
        if prev_x is not None and x - prev_x > 1:
            groups.append(current)
            current = []
        current.append(car)
        prev_x = x
    if current:
        groups.append(current)
    return groups


def load_lines(lines_path: Path) -> dict[KEY, list[dict[str, str]]]:
    by_race: dict[KEY, list[dict[str, str]]] = defaultdict(list)
    with lines_path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            by_race[key(row)].append(row)
    return by_race


def load_filtered(path: Path, keys: set[KEY]) -> dict[KEY, list[dict[str, str]]]:
    by_race: dict[KEY, list[dict[str, str]]] = defaultdict(list)
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            k = key(row)
            if k in keys:
                by_race[k].append(row)
    return by_race


def scan_result_distribution(results_path: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    with results_path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            v = row.get("in_line_jyuni", "")
            counts[v if v != "" else "(blank)"] += 1
    return counts


def scan_result_nonblank_profile(results_path: Path) -> Counter[str]:
    profile: Counter[str] = Counter()
    with results_path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            v = row.get("in_line_jyuni", "")
            if v == "":
                continue
            tyaku = row.get("tyaku", "")
            state = row.get("kojin_state", "") or "(blank_state)"
            note = row.get("tyaku_note", "") or "(blank_note)"
            profile[f"tyaku={'blank' if tyaku == '' else 'nonblank'}"] += 1
            profile[f"state={state}"] += 1
            profile[f"note={note}"] += 1
    return profile


def infer_groups_from_results_and_entries(
    result_rows: list[dict[str, str]],
    entry_rows: list[dict[str, str]],
) -> list[list[int]]:
    """かなり単純な検証用ヒューリスティック。

    - in_line_jyuni がある場合: 同じ値ごとに grouped として扱う
    - 無い場合: 地区ざっくり + 逃/両を先頭候補、追を後ろ候補にする

    この関数の目的は「簡単な逆引きで実用域に届くか」の下限確認。
    """
    by_in_line: dict[int, list[int]] = defaultdict(list)
    for r in result_rows:
        car = to_int(r["car_no"])
        pos = to_int(r.get("in_line_jyuni", ""))
        if car is not None and pos is not None:
            by_in_line[pos].append(car)
    if by_in_line:
        return [cars for _, cars in sorted(by_in_line.items())]

    region_map = {
        "北海道": "north", "青 森": "north", "岩 手": "north", "宮 城": "north",
        "秋 田": "north", "山 形": "north", "福 島": "north",
        "茨 城": "kanto", "栃 木": "kanto", "群 馬": "kanto", "埼 玉": "kanto",
        "千 葉": "kanto", "東 京": "kanto", "神奈川": "kanto", "山 梨": "kanto",
        "新 潟": "koshin", "長 野": "koshin",
        "富 山": "chubu", "石 川": "chubu", "福 井": "chubu", "岐 阜": "chubu",
        "静 岡": "chubu", "愛 知": "chubu", "三 重": "chubu",
        "滋 賀": "kinki", "京 都": "kinki", "大 阪": "kinki", "兵 庫": "kinki",
        "奈 良": "kinki", "和歌山": "kinki",
        "鳥 取": "chugoku", "島 根": "chugoku", "岡 山": "chugoku",
        "広 島": "chugoku", "山 口": "chugoku",
        "徳 島": "shikoku", "香 川": "shikoku", "愛 媛": "shikoku", "高 知": "shikoku",
        "福 岡": "kyushu", "佐 賀": "kyushu", "長 崎": "kyushu", "熊 本": "kyushu",
        "大 分": "kyushu", "宮 崎": "kyushu", "鹿児島": "kyushu", "沖 縄": "kyushu",
    }
    items: list[tuple[str, int, int, int]] = []
    for e in entry_rows:
        car = to_int(e["car_no"])
        if car is None:
            continue
        pref = e.get("prefecture", "")
        region = region_map.get(pref, pref or "unknown")
        kyaku = e.get("kyaku", "")
        leader_rank = 0 if kyaku in ("逃", "両") else 1
        car_rank = car
        items.append((region, leader_rank, car_rank, car))
    grouped: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    for region, leader_rank, car_rank, car in items:
        grouped[region].append((leader_rank, car_rank, car))
    groups = []
    for region in sorted(grouped):
        cars = [car for _, _, car in sorted(grouped[region])]
        groups.append(cars)
    return groups


def main() -> None:
    args = parse_args()
    data_dir = Path(args.data_dir)
    lines_by_race = load_lines(data_dir / "race_lines.csv")
    validation_keys = set(lines_by_race)
    results_by_race = load_filtered(data_dir / "race_results.csv", validation_keys)
    entries_by_race = load_filtered(data_dir / "race_entries.csv", validation_keys)
    in_line_counts = scan_result_distribution(data_dir / "race_results.csv")
    in_line_profile = scan_result_nonblank_profile(data_dir / "race_results.csv")

    y_counts = Counter(r["narabi_y"] for rows in lines_by_race.values() for r in rows)
    line_type_counts = Counter(
        rows[0].get("line_type", "") for rows in entries_by_race.values() if rows
    )

    exact = 0
    pair_hits = 0
    pair_total_pred = 0
    pair_total_gold = 0
    pair_correct = 0
    race_rows = []
    for k in sorted(validation_keys):
        gold = line_groups_from_x(lines_by_race[k])
        pred = infer_groups_from_results_and_entries(
            results_by_race.get(k, []),
            entries_by_race.get(k, []),
        )
        gold_norm = normalize_groups(gold)
        pred_norm = normalize_groups(pred)
        if gold_norm == pred_norm:
            exact += 1

        def pairs(groups: list[list[int]]) -> set[tuple[int, int]]:
            out = set()
            for g in groups:
                for i, a in enumerate(g):
                    for b in g[i + 1:]:
                        out.add(tuple(sorted((a, b))))
            return out

        gp = pairs(gold)
        pp = pairs(pred)
        pair_total_gold += len(gp)
        pair_total_pred += len(pp)
        pair_correct += len(gp & pp)
        if pp and gp & pp:
            pair_hits += 1
        race_rows.append((k, gold, pred, gold_norm == pred_norm, len(gp & pp), len(gp), len(pp)))

    print("=== line_reconstruction_sample ===")
    print(f"validation_races={len(validation_keys)}")
    print(f"validation_rows={sum(len(v) for v in lines_by_race.values())}")
    print("narabi_y_counts=" + ", ".join(f"{k}:{v}" for k, v in sorted(y_counts.items())))
    print("line_type_counts=" + ", ".join(f"{k or '(blank)'}:{v}" for k, v in line_type_counts.most_common()))
    print()
    print("in_line_jyuni_counts_top=" + ", ".join(f"{k}:{v}" for k, v in in_line_counts.most_common(12)))
    print("in_line_nonblank_profile_top=" + ", ".join(f"{k}:{v}" for k, v in in_line_profile.most_common(12)))
    print()
    precision = pair_correct / pair_total_pred if pair_total_pred else 0.0
    recall = pair_correct / pair_total_gold if pair_total_gold else 0.0
    print(f"x_gap_gold_exact_from_naive_pred={exact}/{len(validation_keys)}")
    print(f"same_line_pair_precision={precision:.3f}")
    print(f"same_line_pair_recall={recall:.3f}")
    print(f"races_with_any_pair_hit={pair_hits}/{len(validation_keys)}")
    print()
    print("first_12_races:")
    for k, gold, pred, ok, inter, gold_n, pred_n in race_rows[:12]:
        print(
            f"{k[0]} pc={k[1]} R{k[2]} exact={ok} "
            f"pair_hit={inter}/{gold_n} pred_pairs={pred_n} "
            f"gold={gold} pred={pred}"
        )


if __name__ == "__main__":
    main()
