"""Baseline route validation: planned routes vs historical voyages.

For every historical voyage the router is run on the same origin, destination
and vessel class, against the same ship snapshot, and the two tracks are
measured by the same function (routing.track_metrics). The comparison is
paired, which removes the variance from route and vessel mix.

Distance is compared directly. "Reliability" is measured as the things that
make a passage unsafe or unusable:

    obstacles_met       stationary ships passed inside their target CPA
    min_ship_clear_nm   closest approach to any ship's planning envelope
    hard_breaches       ships passed inside the HARD passing limit
    shallow_nm          NM sailed where depth < draught + UKC
    min_shore_nm        closest approach to land
    feasible            no land, no shallow water, no hard breach

Caveat that belongs next to every number: a synthetic baseline corpus is
generated without knowledge of the ship snapshot, and a real AIS voyage was
sailed among the ships present at ITS time, not at the snapshot's. The
comparison measures what planning against a given snapshot buys, and the
reliability of the baseline is its reliability against that snapshot.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy import stats

from . import study_area as SA
from .ais import Voyage
from .network import NavNetwork
from .obstacles import Snapshot
from .routing import plan_route, track_metrics

METRICS = ["distance_nm", "obstacles_met", "min_ship_clear_nm", "hard_breaches",
           "shallow_nm", "min_shore_nm", "nm_inside_cpa"]
# direction in which a metric improves: +1 lower is better, -1 higher is better
BETTER = {"distance_nm": 1, "obstacles_met": 1, "min_ship_clear_nm": -1,
          "hard_breaches": 1, "shallow_nm": 1, "min_shore_nm": -1, "nm_inside_cpa": 1}


def compare_to_baseline(nets: Dict[Tuple[str, str], NavNetwork],
                        voyages: Sequence[Voyage], snap: Snapshot,
                        modes: Sequence[str] = ("shortest", "safest"),
                        max_voyages: Optional[int] = None) -> pd.DataFrame:
    """`nets` maps (vessel, mode) -> built network."""
    rows: List[Dict] = []
    plan_cache: Dict[Tuple, Dict] = {}
    pool = [v for v in voyages if v.origin in SA.PORTS and v.destination in SA.PORTS]
    if max_voyages:
        pool = pool[:max_voyages]
    for v in pool:
        any_net = nets.get((v.vessel_class, modes[0]))
        if any_net is None:
            continue
        bm, _ = track_metrics(any_net, v.track, snap, max_list=0)
        row = dict(voyage_id=v.voyage_id, origin=v.origin, destination=v.destination,
                   vessel_class=v.vessel_class, synthetic=v.synthetic)
        for k in METRICS + ["feasible"]:
            row[f"base_{k}"] = bm[k]
        for mode in modes:
            key = (v.vessel_class, mode, v.origin, v.destination)
            if key not in plan_cache:
                net = nets[(v.vessel_class, mode)]
                ps = plan_route(net, v.origin, v.destination, snap)
                plan_cache[key] = ps[0].metrics if ps else None
            pm = plan_cache[key]
            if pm is None:
                continue
            for k in METRICS + ["feasible"]:
                row[f"{mode}_{k}"] = pm[k]
        rows.append(row)
    return pd.DataFrame(rows)


def paired_tests(df: pd.DataFrame, mode: str,
                 metrics: Sequence[str] = METRICS) -> pd.DataFrame:
    """Paired t-test and Wilcoxon signed-rank, baseline vs `mode`.

    `improvement` is signed so that positive always means the planned route is
    better (shorter, fewer obstacles, more clearance...).
    """
    out = []
    for m in metrics:
        b, o = f"base_{m}", f"{mode}_{m}"
        if b not in df or o not in df:
            continue
        sub = df[[b, o]].dropna()
        x, y = sub[b].to_numpy(float), sub[o].to_numpy(float)
        if len(x) < 3:
            continue
        d = (x - y) * BETTER[m]
        row = dict(mode=mode, metric=m, n=len(d), baseline_mean=x.mean(),
                   planned_mean=y.mean(), mean_improvement=d.mean(),
                   pct_improvement=100 * d.mean() / abs(x.mean()) if x.mean() else np.nan)
        if np.allclose(d, 0):
            row.update(t_p=np.nan, wilcoxon_p=np.nan, cohens_dz=0.0)
        else:
            row["t_p"] = stats.ttest_rel(x, y).pvalue
            try:
                row["wilcoxon_p"] = stats.wilcoxon(x, y).pvalue
            except ValueError:
                row["wilcoxon_p"] = np.nan
            sd = d.std(ddof=1)
            row["cohens_dz"] = d.mean() / sd if sd > 0 else np.nan
        out.append(row)
    return pd.DataFrame(out)


def reliability_summary(df: pd.DataFrame, modes: Sequence[str]) -> pd.DataFrame:
    """Share of voyages that are feasible / breach-free, per approach."""
    rows = []
    for who in ["base"] + list(modes):
        if f"{who}_feasible" not in df:
            continue
        rows.append(dict(
            approach="historical baseline" if who == "base" else f"planned ({who})",
            n=int(df[f"{who}_feasible"].notna().sum()),
            feasible_pct=100 * df[f"{who}_feasible"].mean(),
            hard_breach_free_pct=100 * (df[f"{who}_hard_breaches"] == 0).mean(),
            zero_obstacles_met_pct=100 * (df[f"{who}_obstacles_met"] == 0).mean(),
            mean_distance_nm=df[f"{who}_distance_nm"].mean(),
            mean_obstacles_met=df[f"{who}_obstacles_met"].mean(),
            mean_min_ship_clear_nm=df[f"{who}_min_ship_clear_nm"].mean(),
        ))
    return pd.DataFrame(rows)


def by_route(df: pd.DataFrame, modes: Sequence[str]) -> pd.DataFrame:
    agg = {"voyage_id": "count", "base_distance_nm": "mean", "base_obstacles_met": "mean"}
    for m in modes:
        agg[f"{m}_distance_nm"] = "mean"
        agg[f"{m}_obstacles_met"] = "mean"
    out = df.groupby(["origin", "destination", "vessel_class"]).agg(agg).reset_index()
    return out.rename(columns={"voyage_id": "n"}).round(3)
