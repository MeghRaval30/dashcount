"""Tuning report: what-if tables for the count gate, the Kronos veto and the cost hazard.

Works on one practice run's flight recorder (attempts + outcomes, ghosts included).
Every setup is scored in R *after estimated costs*: a ghost's R is measured on the
price path with no friction, so the round-trip cost in R (target is 2R, so cost =
2R / target_to_cost) is subtracted. Real trades already pay costs in their fills.

Each what-if says: "if every setup that passed the other filters had been taken at
1R, how much would this threshold have made?" Results are split into an early
(tune) and a late (check) part of the run, so a threshold is only trusted if it
helps in both halves.

    uv run --no-sync python scripts/tuning_report.py data/records/<run> [--split 2026-08-17]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dashcount.recorder import read_table  # noqa: E402

TARGET_R = 2.0


def load(run_dir: Path) -> pd.DataFrame:
    att = read_table(run_dir, "attempts")
    out = read_table(run_dir, "outcomes")
    keep = ["attempt_id", "is_ghost", "exit_reason", "r", "mfe_r", "mae_r"] + (["r_net"] if "r_net" in out else [])
    j = att.merge(out[keep], on="attempt_id", how="left")
    j["hazard_list"] = j["hazards"].map(lambda h: json.loads(h) if isinstance(h, str) and h else [])
    ttc = pd.to_numeric(j["target_to_cost"], errors="coerce")
    j["cost_r"] = np.where(ttc > 0, TARGET_R / ttc, np.nan)
    # real trades already paid costs in their fills; ghosts did not (newer runs record r_net)
    net = j["r_net"] if "r_net" in j else j["r"] - j["cost_r"].fillna(0.0)
    j["net_r"] = np.where(j["is_ghost"] == True, net, j["r"])  # noqa: E712
    j["stage"] = j["action"].map(lambda a: str(a).split(":")[0])
    j["bet_mult"] = np.select([j["tc"] >= 3, j["tc"] >= 2, j["tc"] >= 1], [4.0, 2.0, 1.0], 1.0)
    return j


def stats(r: pd.Series) -> dict:
    r = r.dropna()
    n = len(r)
    if not n:
        return {"n": 0, "avg_r": None, "se": None, "win": None, "total_r": 0.0}
    se = float(r.std(ddof=1) / math.sqrt(n)) if n > 1 else None
    return {"n": n, "avg_r": round(float(r.mean()), 3), "se": None if se is None else round(se, 3),
            "win": round(float((r > 0).mean()), 2), "total_r": round(float(r.sum()), 1)}


def table(rows: list[dict], title: str) -> str:
    if not rows:
        return f"\n### {title}\n(no data)\n"
    cols = list(rows[0].keys())
    out = [f"\n### {title}", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join("" if r[c] is None else str(r[c]) for c in cols) + " |")
    return "\n".join(out) + "\n"


def by_bins(df: pd.DataFrame, col: str, edges: list[float], labels: list[str]) -> list[dict]:
    b = pd.cut(df[col], edges, labels=labels, right=False)
    return [{"bin": str(k), **stats(g["net_r"])} for k, g in df.groupby(b, observed=False)]


def what_if(df: pd.DataFrame, col: str, thresholds: list[float], keep, split) -> list[dict]:
    rows = []
    for t in thresholds:
        k = df[keep(df[col], t)]
        early, late = k[k["day"] < split], k[k["day"] >= split]
        s = stats(k["net_r"])
        rows.append({"threshold": t, "n": s["n"], "avg_r": s["avg_r"], "total_r": s["total_r"],
                     "bet_weighted_r": round(float((k["net_r"] * k["bet_mult"]).sum()), 1),
                     "early_total_r": stats(early["net_r"])["total_r"], "late_total_r": stats(late["net_r"])["total_r"]})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--split", help="first day of the 'check' part (default: two thirds of the way in)")
    ap.add_argument("--out", type=Path, help="also write the report to this Markdown file")
    a = ap.parse_args()

    j = load(a.run_dir)
    days = sorted(j["day"].astype(str).unique())
    split = a.split or days[int(len(days) * 2 / 3)]
    j["day"] = j["day"].astype(str)
    L = [f"# Tuning report: {a.run_dir.name}",
         f"{len(days)} days ({days[0]} .. {days[-1]}), {len(j)} setups. Tune part: before {split}; check part: from {split}.",
         "R is after estimated costs. 'se' is the standard error of avg_r: an avg_r within ~2 se of 0 is noise."]

    L.append(table([{"decision": k, "n": int(v)} for k, v in j["action"].value_counts().items()], "Where setups ended up"))

    # --- the count ---------------------------------------------------------
    clean = j[(j["stage"] != "skip_hazard")]          # passed every hazard; count gate decides next
    edges = [-99, 0, 0.5, 1, 1.5, 2, 3, 99]
    labels = ["<0", "0-0.5", "0.5-1", "1-1.5", "1.5-2", "2-3", "3+"]
    L.append(table(by_bins(clean, "tc", edges, labels),
                   "Is the count predictive? Hazard-free setups by true count (before vetoes)"))
    L.append(table(by_bins(clean[clean["day"] < split], "tc", edges, labels), "  ... tune part only"))
    L.append(table(by_bins(clean[clean["day"] >= split], "tc", edges, labels), "  ... check part only"))
    L.append(table(what_if(clean, "tc", [0.0, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0], lambda c, t: c >= t, split),
                   "What if gates.min_true_count = threshold (hazard-free setups, vetoes ignored)"))
    for pad, g in clean.groupby("pad"):
        L.append(table(by_bins(g, "tc", edges, labels), f"  ... pad {pad}"))
    if "tc_alt" in clean and clean["tc_alt"].notna().any():
        L.append(table(by_bins(clean, "tc_alt", edges, labels),
                       "The other signing rule's count (tc_alt, recorded only) vs result"))
        L.append(table(what_if(clean, "tc_alt", [0.0, 0.5, 1.0, 1.5, 2.0], lambda c, t: c >= t, split),
                       "What if the gate used tc_alt instead"))

    # --- Kronos ---------------------------------------------------------------
    kr = j[j["kronos_odds"].notna()]
    L.append(table(by_bins(kr, "kronos_odds", [0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 1.01],
                           ["0-.2", ".2-.3", ".3-.4", ".4-.5", ".5-.6", ".6-.7", ".7+"]),
                   "Kronos odds vs result (setups that reached Kronos)"))
    L.append(table(what_if(kr, "kronos_odds", [0.0, 0.3, 0.35, 0.4, 0.45, 0.5], lambda c, t: c >= t, split),
                   "What if veto.kronos.min_odds = threshold (Fin-R1 ignored)"))

    # --- Fin-R1 ---------------------------------------------------------------
    fr = j[j["finr1_verdict"].notna()]
    if len(fr):
        L.append(table([{"verdict": k, **stats(g["net_r"])} for k, g in fr.groupby("finr1_verdict")],
                       "Fin-R1 verdict vs result"))
        L.append(table([{"reason": k, **stats(g["net_r"])} for k, g in fr.groupby(fr["finr1_reason"].fillna("-"))],
                       "  ... by Fin-R1 reason"))

    # --- cost hazard ------------------------------------------------------------
    only_cost = j[j["hazard_list"].map(lambda h: h == ["too_costly"])]
    others_ok = pd.concat([clean, only_cost])
    others_ok = others_ok[others_ok["tc"] >= 1.0]       # would also have passed today's count gate
    L.append(table(by_bins(others_ok, "target_to_cost", [0, 2, 3, 4, 5, 6, 8, 1e9],
                           ["<2", "2-3", "3-4", "4-5", "5-6", "6-8", "8+"]),
                   "Target-to-cost ratio vs result (no other hazard, true count >= 1)"))
    L.append(table(what_if(others_ok, "target_to_cost", [2.0, 3.0, 4.0, 5.0, 6.0], lambda c, t: c >= t, split),
                   "What if hazards.cost_mult = threshold"))

    # --- other hazards (ghost value of each skip reason) ----------------------------
    L.append(table([{"skip": k, **stats(g["net_r"])} for k, g in j.groupby("action")],
                   "Every decision, scored by what the setup did next"))

    txt = "\n".join(L)
    print(txt)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(txt, encoding="utf-8")


if __name__ == "__main__":
    main()
