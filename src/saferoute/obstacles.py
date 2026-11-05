"""Static ship obstacles: the "pieces" on the board.

A snapshot is the set of vessels that are NOT under way at one moment -
anchored, moored or otherwise stationary - with their positions and sizes.
The router treats each one as a physical obstacle.

Where a snapshot comes from
---------------------------
1. A real AIS capture (scripts/capture_ais_snapshot.py, aisstream.io) written
   as CSV in the SNAPSHOT_COLUMNS schema below -> `load_snapshot_csv`.
2. Any AIS extract with the usual columns (MMSI, time, lat, lon, SOG, nav
   status...), e.g. the sibling group's cleaned AIS -> `snapshot_from_ais_table`,
   which picks each vessel's last report in a time window and keeps the
   stationary ones.
3. A SYNTHETIC snapshot (`synthetic_snapshot`): ships placed inside MPA's real
   anchorages (study_area.ANCHORAGE_POINTS) with realistic sizes and a common
   heading to the tidal stream. For developing and demonstrating the method
   only; it is labelled synthetic everywhere it appears and carries no MMSIs.

A note on "container ship": AIS ship-type codes 70-79 mean "cargo" and do not
separate container ships from bulk carriers or general cargo. A real snapshot
therefore reports AIS categories; container-only filtering needs an external
register (IMO number -> ship type) and is not attempted.

How much water a stationary ship occupies
-----------------------------------------
* Moored: the hull, a circle of radius LOA/2 around the position.
* Anchored: the ship swings round its anchor with the tidal stream, so the
  water it can occupy is the swing circle: radius = LOA + horizontal scope of
  cable, centred on the anchor. The anchor lies ahead of the AIS antenna
  (bridge ~0.8 LOA aft of the bow) by the horizontal scope. Scope follows the
  common rule of thumb  shackles = 1.5 * sqrt(depth_m),  1 shackle = 27.5 m.
  Planning against the swing circle rather than the hull is what makes a
  route stay valid when the tide turns.
* Stationary but not reported anchored/moored: hull + 100 m.
"""
from __future__ import annotations

import csv
import io
import math
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from . import study_area as SA
from .geo import densify, M_PER_NM, haversine_nm

SNAPSHOT_COLUMNS = ["id", "mmsi", "name", "ship_type", "ais_type", "lat", "lon",
                    "sog_kn", "heading_deg", "nav_status", "status", "loa_m",
                    "beam_m", "draught_m", "timestamp_utc", "area", "source"]

SHIP_TYPES = ["container", "tanker", "bulker", "cargo", "gas", "passenger",
              "service", "other"]

# Nominal dimensions when a report lacks them: (LOA, beam, draught).
DEFAULT_DIMS = {
    "container": (260, 32, 12), "tanker": (183, 32, 11), "bulker": (190, 32, 11),
    "cargo": (160, 25, 9), "gas": (230, 36, 11), "passenger": (120, 20, 5),
    "service": (40, 12, 4), "other": (100, 18, 6),
}
SHACKLE_M = 27.5


def ais_type_to_category(code) -> str:
    """ITU-R M.1371 ship-and-cargo type -> the categories used here."""
    try:
        c = int(float(code))
    except (TypeError, ValueError):
        return "other"
    if 70 <= c <= 79:
        return "cargo"
    if 80 <= c <= 89:
        return "tanker"
    if 60 <= c <= 69:
        return "passenger"
    if c in (31, 32, 33, 34, 35, 50, 51, 52, 53, 54, 55, 58, 59) or 30 <= c <= 39:
        return "service"
    return "other"


def nav_status_to_state(ns) -> str:
    try:
        n = int(float(ns))
    except (TypeError, ValueError):
        return "stationary"
    return {1: "anchored", 5: "moored", 6: "aground"}.get(n, "stationary")


@dataclass
class ShipObstacle:
    id: str
    lat: float
    lon: float
    loa_m: float
    beam_m: float
    draught_m: float
    heading_deg: float = 0.0
    ship_type: str = "other"
    status: str = "anchored"          # anchored | moored | stationary | aground
    name: str = ""
    mmsi: Optional[int] = None
    ais_type: Optional[int] = None
    nav_status: Optional[int] = None
    sog_kn: float = 0.0
    timestamp_utc: str = ""
    area: str = ""
    source: str = "synthetic"
    depth_hint_m: float = 20.0        # water depth used for the cable scope

    # -- geometry ---------------------------------------------------------
    def scope_m(self) -> float:
        shackles = 1.5 * math.sqrt(max(self.depth_hint_m, 5.0))
        chain = shackles * SHACKLE_M
        return math.sqrt(max(chain ** 2 - self.depth_hint_m ** 2, 0.0))

    def centre(self) -> Tuple[float, float]:
        """Centre of the water this ship can occupy (the anchor if anchored)."""
        if self.status != "anchored":
            return self.lat, self.lon
        ahead = 0.8 * self.loa_m + self.scope_m()          # metres ahead of the antenna
        h = math.radians(self.heading_deg)
        dlat = ahead * math.cos(h) / 111_320.0
        dlon = ahead * math.sin(h) / (111_320.0 * math.cos(math.radians(self.lat)))
        return self.lat + dlat, self.lon + dlon

    def radius_m(self, swing: bool = True) -> float:
        if self.status == "anchored" and swing:
            return self.loa_m + self.scope_m()
        if self.status == "moored":
            return self.loa_m / 2.0
        return self.loa_m / 2.0 + 100.0

    def envelope(self, swing: bool = True) -> Tuple[float, float, float]:
        """(lat, lon, radius_nm) of the obstacle circle actually planned around."""
        if swing and self.status == "anchored":
            la, lo = self.centre()
        else:
            la, lo = self.lat, self.lon
        return la, lo, self.radius_m(swing) / M_PER_NM

    def as_dict(self) -> Dict:
        d = asdict(self)
        d.pop("depth_hint_m", None)
        return d


@dataclass
class Snapshot:
    ships: List[ShipObstacle]
    source: str                       # "synthetic" | "ais" | file name
    label: str = ""
    captured_utc: str = ""
    notes: List[str] = field(default_factory=list)

    def __len__(self):
        return len(self.ships)

    def filtered(self, types: Optional[Sequence[str]] = None) -> "Snapshot":
        if not types:
            return self
        keep = [s for s in self.ships if s.ship_type in set(types)]
        return Snapshot(keep, self.source, self.label, self.captured_utc, list(self.notes))

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for s in self.ships:
            out[s.ship_type] = out.get(s.ship_type, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# --------------------------------------------------------------------------
# reading and writing snapshots
# --------------------------------------------------------------------------
def _f(v, default=None):
    try:
        if v is None or (isinstance(v, str) and not v.strip()):
            return default
        x = float(v)
        return default if math.isnan(x) else x
    except (TypeError, ValueError):
        return default


def ship_from_record(r: Dict, source: str = "ais") -> Optional[ShipObstacle]:
    lat, lon = _f(r.get("lat")), _f(r.get("lon"))
    if lat is None or lon is None:
        return None
    stype = (r.get("ship_type") or "").strip().lower()
    ais_type = _f(r.get("ais_type"))
    if not stype:
        stype = ais_type_to_category(ais_type)
    if stype not in DEFAULT_DIMS:
        stype = "other"
    dl, db, dd = DEFAULT_DIMS[stype]
    loa = _f(r.get("loa_m"), 0) or dl
    beam = _f(r.get("beam_m"), 0) or db
    draught = _f(r.get("draught_m"), 0) or dd
    ns = _f(r.get("nav_status"))
    status = (r.get("status") or "").strip().lower() or nav_status_to_state(ns)
    hdg = _f(r.get("heading_deg"))
    if hdg is None or hdg >= 360:          # 511 = not available
        hdg = _f(r.get("cog_deg"), 0.0) or 0.0
        if hdg >= 360:
            hdg = 0.0
    mmsi = _f(r.get("mmsi"))
    return ShipObstacle(
        id=str(r.get("id") or (int(mmsi) if mmsi else f"ship-{lat:.4f}-{lon:.4f}")),
        lat=lat, lon=lon, loa_m=loa, beam_m=beam, draught_m=draught,
        heading_deg=float(hdg) % 360.0, ship_type=stype, status=status,
        name=(r.get("name") or "").strip(), mmsi=int(mmsi) if mmsi else None,
        ais_type=int(ais_type) if ais_type is not None else None,
        nav_status=int(ns) if ns is not None else None,
        sog_kn=_f(r.get("sog_kn"), 0.0), timestamp_utc=r.get("timestamp_utc") or "",
        area=r.get("area") or "", source=r.get("source") or source)


def load_snapshot_csv(path_or_text: str, source: Optional[str] = None,
                      is_text: bool = False, grid=None) -> Snapshot:
    """Read a snapshot written in SNAPSHOT_COLUMNS (or anything close to it).

    If the file is instead a raw AIS table (MMSI/ReceiveTime/SoG... columns),
    it is routed through `snapshot_from_ais_table` automatically.
    """
    import pandas as pd
    df = pd.read_csv(io.StringIO(path_or_text) if is_text else path_or_text)
    cols = {c.lower() for c in df.columns}
    if "lat" not in cols and "latitude" not in cols:
        raise ValueError("snapshot needs lat/lon columns")
    if not {"loa_m", "status", "ship_type"} & cols:
        return snapshot_from_ais_table(df, source=source or "ais table", grid=grid)
    df.columns = [c.lower() for c in df.columns]
    ships = []
    for r in df.to_dict("records"):
        s = ship_from_record({k: (None if (isinstance(v, float) and math.isnan(v)) else v)
                              for k, v in r.items()}, source=source or "ais")
        if s is not None:
            ships.append(s)
    _depth_hints(ships, grid)
    stamps = sorted({s.timestamp_utc for s in ships if s.timestamp_utc})
    return Snapshot(ships, source or "ais", label=source or "AIS snapshot",
                    captured_utc=stamps[-1] if stamps else "")


def write_snapshot_csv(snap: Snapshot, path: str) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=SNAPSHOT_COLUMNS)
        w.writeheader()
        for s in snap.ships:
            d = s.as_dict()
            w.writerow({k: d.get(k, "") if d.get(k) is not None else "" for k in SNAPSHOT_COLUMNS})


ALIASES = {
    "mmsi": ["mmsi"],
    "time": ["receivetime", "timestamp", "time", "basedatetime", "time_utc",
             "timestamp_utc", "datetime", "t"],
    "lat": ["lat", "latitude"],
    "lon": ["lon", "lng", "long", "longitude"],
    "sog": ["sog", "sog_kn", "speed", "speedoverground"],
    "cog": ["cog", "cog_deg", "courseoverground"],
    "heading": ["heading", "heading_deg", "trueheading"],
    "nav": ["navstatus", "nav_status", "status", "navigationalstatus"],
    "type": ["shiptype", "ship_type", "vesseltype", "type", "ais_type"],
    "length": ["length", "loa", "loa_m", "length_m"],
    "width": ["width", "beam", "beam_m", "width_m"],
    "draught": ["draught", "draft", "draught_m"],
    "name": ["name", "vesselname", "shipname"],
}


def _pick(df, key):
    lower = {c.lower().replace(" ", ""): c for c in df.columns}
    for a in ALIASES[key]:
        if a in lower:
            return lower[a]
    return None


def snapshot_from_ais_table(df, at=None, window_min: float = 30.0,
                            sog_max_kn: float = 0.5, source: str = "ais table",
                            grid=None) -> Snapshot:
    """Stationary vessels from a raw AIS table.

    Takes each MMSI's last report within `window_min` before `at` (default: the
    latest time in the table) and keeps it if it is anchored (NavStatus 1),
    moored (5), or moving slower than `sog_max_kn`. Works on the column layout
    of an AIS extract (ReceiveTime, MMSI, NavStatus, RoT, SoG, Lat, Lon, CoG,
    Heading) and on MarineCadastre-style names (BaseDateTime, LAT, LON...).
    """
    import pandas as pd
    c = {k: _pick(df, k) for k in ALIASES}
    if not c["lat"] or not c["lon"]:
        raise ValueError("AIS table needs latitude and longitude columns")
    d = df.copy()
    notes = []
    if c["time"]:
        d["_t"] = pd.to_datetime(d[c["time"]], errors="coerce", utc=True)
        t_end = pd.to_datetime(at, utc=True) if at is not None else d["_t"].max()
        d = d[(d["_t"] <= t_end) & (d["_t"] >= t_end - pd.Timedelta(minutes=window_min))]
        d = d.sort_values("_t")
        notes.append(f"last report per vessel in the {window_min:.0f} min to {t_end}")
    key = c["mmsi"] or c["lat"]
    last = d.groupby(key, sort=False).tail(1) if c["mmsi"] else d
    ships = []
    moving = 0
    for r in last.to_dict("records"):
        sog = _f(r.get(c["sog"]), 0.0) if c["sog"] else 0.0
        ns = _f(r.get(c["nav"])) if c["nav"] else None
        stationary = (ns in (1, 5, 6)) or sog <= sog_max_kn
        if not stationary:
            moving += 1
            continue
        rec = dict(mmsi=r.get(c["mmsi"]) if c["mmsi"] else None,
                   lat=r[c["lat"]], lon=r[c["lon"]], sog_kn=sog,
                   heading_deg=r.get(c["heading"]) if c["heading"] else None,
                   cog_deg=r.get(c["cog"]) if c["cog"] else None,
                   nav_status=ns,
                   ais_type=r.get(c["type"]) if c["type"] else None,
                   loa_m=r.get(c["length"]) if c["length"] else None,
                   beam_m=r.get(c["width"]) if c["width"] else None,
                   draught_m=r.get(c["draught"]) if c["draught"] else None,
                   name=str(r.get(c["name"]) or "") if c["name"] else "",
                   timestamp_utc=str(r.get("_t") or ""), source="ais")
        tval = rec["ais_type"]
        if tval is not None and not str(tval).replace(".", "").isdigit():
            rec["ship_type"] = str(tval).lower()
            rec["ais_type"] = None
        s = ship_from_record(rec, source="ais")
        if s is not None and (grid is None or grid.inside(s.lat, s.lon)):
            ships.append(s)
    notes.append(f"{len(ships)} stationary kept, {moving} under way dropped "
                 f"(SOG > {sog_max_kn} kn and not anchored/moored)")
    _depth_hints(ships, grid)
    return Snapshot(ships, "ais", label=source, notes=notes,
                    captured_utc=str(d["_t"].max()) if c["time"] and len(d) else "")


def _depth_hints(ships, grid) -> None:
    if grid is None:
        return
    for s in ships:
        iy, ix = grid.to_index(s.lat, s.lon)
        s.depth_hint_m = float(np.clip(grid.depth_m[iy, ix], 8.0, 40.0))


# --------------------------------------------------------------------------
# aisstream.io message parsing (used by scripts/capture_ais_snapshot.py)
# --------------------------------------------------------------------------
def merge_aisstream_message(state: Dict[int, Dict], msg: Dict) -> Optional[int]:
    """Fold one aisstream.io JSON message into a per-MMSI state dict.

    Position reports update position/speed/status; static data updates name,
    type and dimensions (A+B = length, C+D = beam). Returns the MMSI touched.
    """
    mt = msg.get("MessageType")
    meta = msg.get("MetaData") or msg.get("Metadata") or {}
    body = (msg.get("Message") or {}).get(mt) or {}
    mmsi = meta.get("MMSI") or body.get("UserID")
    if not mmsi:
        return None
    mmsi = int(mmsi)
    st = state.setdefault(mmsi, {"mmsi": mmsi, "max_sog": 0.0, "n_pos": 0})
    if meta.get("ShipName"):
        st["name"] = str(meta["ShipName"]).strip()
    if mt in ("PositionReport", "StandardClassBPositionReport",
              "ExtendedClassBPositionReport"):
        st["lat"] = body.get("Latitude", meta.get("latitude"))
        st["lon"] = body.get("Longitude", meta.get("longitude"))
        sog = body.get("Sog")
        st["sog_kn"] = sog
        if sog is not None and sog < 102.3:
            st["max_sog"] = max(st["max_sog"], float(sog))
        st["cog_deg"] = body.get("Cog")
        st["heading_deg"] = body.get("TrueHeading")
        if "NavigationalStatus" in body:
            st["nav_status"] = body.get("NavigationalStatus")
        st["timestamp_utc"] = meta.get("time_utc", "")
        st["n_pos"] += 1
    elif mt == "ShipStaticData":
        st["ais_type"] = body.get("Type")
        dim = body.get("Dimension") or {}
        a, b, c, d = (dim.get(k) or 0 for k in ("A", "B", "C", "D"))
        if a + b > 0:
            st["loa_m"] = a + b
        if c + d > 0:
            st["beam_m"] = c + d
        if body.get("MaximumStaticDraught"):
            st["draught_m"] = body["MaximumStaticDraught"]
        if body.get("Name"):
            st["name"] = str(body["Name"]).strip()
    return mmsi


def snapshot_from_stream_state(state: Dict[int, Dict], sog_max_kn: float = 0.5,
                               grid=None) -> Snapshot:
    ships, moving = [], 0
    for st in state.values():
        if st.get("lat") is None:
            continue
        ns = st.get("nav_status")
        if not (ns in (1, 5, 6) or st.get("max_sog", 99) <= sog_max_kn):
            moving += 1
            continue
        s = ship_from_record(dict(st, source="ais"), source="ais")
        if s is not None and (grid is None or grid.inside(s.lat, s.lon)):
            ships.append(s)
    _depth_hints(ships, grid)
    return Snapshot(ships, "ais", label="aisstream.io capture",
                    notes=[f"{len(ships)} stationary, {moving} under way dropped"])


# --------------------------------------------------------------------------
# synthetic snapshot inside the real MPA anchorages
# --------------------------------------------------------------------------
TYPE_MIX = {
    "petroleum": {"tanker": .82, "gas": .10, "other": .08},
    "bunkering": {"container": .45, "bulker": .22, "tanker": .20, "cargo": .13},
    "holding":   {"container": .42, "bulker": .28, "tanker": .18, "cargo": .12},
    "general":   {"container": .38, "bulker": .27, "tanker": .15, "cargo": .15, "other": .05},
    "small":     {"service": .70, "cargo": .30},
}
AREA_RADIUS_NM = {"petroleum": .75, "bunkering": .65, "holding": .65,
                  "general": .85, "small": .40}
SHIPS_PER_POINT = {"petroleum": 7, "bunkering": 6, "holding": 7, "general": 8,
                   "small": 4}
SIZES = {   # (LOA, beam, draught, weight)
    "container": [(172, 27, 9.0, 3), (200, 32, 10.5, 3), (260, 32, 12.0, 3),
                  (294, 32, 12.5, 3), (300, 48, 13.5, 2), (335, 43, 14.0, 2),
                  (366, 51, 15.2, 2), (400, 59, 16.0, 1)],
    "tanker":    [(120, 20, 7.5, 2), (183, 32, 11.0, 4), (228, 32, 13.0, 3),
                  (250, 44, 14.5, 2), (274, 48, 16.0, 1), (333, 60, 20.5, 1)],
    "bulker":    [(180, 30, 10.0, 2), (190, 32, 12.5, 3), (229, 32, 14.0, 3),
                  (292, 45, 18.0, 1)],
    "cargo":     [(100, 17, 6.0, 3), (120, 20, 7.5, 3), (150, 23, 9.0, 2)],
    "gas":       [(230, 36, 11.0, 2), (290, 46, 12.0, 1)],
    "service":   [(40, 12, 3.5, 3), (60, 15, 4.0, 2), (75, 18, 5.0, 1)],
    "other":     [(90, 16, 5.0, 2), (130, 22, 7.0, 1)],
}


def _choice(rng, weights: Dict[str, float]) -> str:
    keys = list(weights)
    p = np.array([weights[k] for k in keys], float)
    return str(rng.choice(keys, p=p / p.sum()))


def synthetic_snapshot(grid, seed: int = 20260929, density: float = 1.0,
                       stream_heading_deg: float = 72.0,
                       keep_clear_of_approaches_nm: float = 0.6) -> Snapshot:
    """Stationary ships placed inside MPA's anchorages.

    Rejection sampling inside each anchorage disc, subject to: water at least
    0.15 NM off the shore, outside the TSS lanes and separation zone, clear of
    the terminal approaches, pilotage tracks and fairways to the TSS, and
    swing circles that do not overlap an
    existing ship's (MPA's anchorage rules exist to prevent exactly that).
    All ships lie to one tidal stream heading +/- 15 deg, as they do at anchor.
    """
    rng = np.random.default_rng(seed)
    ships: List[ShipObstacle] = []
    # keep clear: the terminal approaches, the pilotage tracks to the berths,
    # and a fairway from each terminal approach to the nearest point of the
    # TSS (anchorages are laid out round the port fairways, not across them)
    from .seagrid import fairway_path
    fair = [pt for c in SA.TERMINALS for pt in densify(fairway_path(c), 0.1)]
    ap = np.array([p.approach for p in SA.PORTS.values()] +
                  [pt for p in SA.PORTS.values() if len(p.pilot) > 1
                   for pt in densify(p.pilot, 0.1)] + fair)
    e_lat: List[float] = []
    e_lon: List[float] = []
    e_r: List[float] = []
    k = 0
    for code, name, charted, alat, alon in SA.ANCHORAGE_POINTS:
        use = SA.anchorage_use(code)
        want = int(round(SHIPS_PER_POINT[use] * density))
        r_nm = AREA_RADIUS_NM[use]
        placed, tries = 0, 0
        while placed < want and tries < 400:
            tries += 1
            rr = r_nm * math.sqrt(rng.uniform())
            th = rng.uniform(0, 2 * math.pi)
            lat = alat + rr * math.cos(th) / 60.0
            lon = alon + rr * math.sin(th) / (60.0 * math.cos(math.radians(alat)))
            if not grid.inside(lat, lon):
                continue
            iy, ix = grid.to_index(lat, lon)
            if grid.land[iy, ix] or grid.dist_to_land_nm[iy, ix] < 0.15:
                continue
            if 1 <= grid.tss_zone[iy, ix] <= 3:
                continue
            if np.min(haversine_nm(lat, lon, ap[:, 0], ap[:, 1])) < keep_clear_of_approaches_nm:
                continue
            stype = _choice(rng, TYPE_MIX[use])
            sz = SIZES[stype]
            w = np.array([s[3] for s in sz], float)
            loa, beam, dr, _ = sz[int(rng.choice(len(sz), p=w / w.sum()))]
            hdg = (stream_heading_deg + rng.normal(0, 7)) % 360
            s = ShipObstacle(
                id=f"SYN-{k + 1:04d}", lat=float(lat), lon=float(lon),
                loa_m=float(loa), beam_m=float(beam), draught_m=float(dr),
                heading_deg=float(hdg), ship_type=stype, status="anchored",
                name=f"Synthetic {stype} {k + 1}", area=code, source="synthetic",
                nav_status=1, depth_hint_m=float(np.clip(grid.depth_m[iy, ix], 8, 40)))
            c_lat, c_lon = s.centre()
            if not grid.inside(c_lat, c_lon):
                continue
            ci, cj = grid.to_index(c_lat, c_lon)
            if grid.land[ci, cj]:
                continue
            r_s = s.radius_m() / M_PER_NM
            if e_lat and np.any(haversine_nm(c_lat, c_lon, np.array(e_lat), np.array(e_lon))
                                < 0.9 * (np.array(e_r) + r_s)):
                continue
            e_lat.append(c_lat); e_lon.append(c_lon); e_r.append(r_s)
            ships.append(s)
            placed += 1
            k += 1
    return Snapshot(ships, "synthetic", label="Synthetic snapshot in MPA anchorages",
                    notes=["SYNTHETIC: positions sampled inside MPA anchorages "
                           "(Singapore Port Information 2025 reference points); "
                           "not real vessels, no MMSIs."])


# --------------------------------------------------------------------------
# the obstacle field on the board
# --------------------------------------------------------------------------
@dataclass
class ObstacleField:
    clear_nm: np.ndarray          # (NY, NX) clearance to the nearest ship envelope, NM
    nearest: np.ndarray           # (NY, NX) index of that ship, -1 if none within range
    envelopes: np.ndarray         # (N, 3) lat, lon, radius_nm
    swing: bool
    max_range_nm: float


def obstacle_field(grid, snap: Snapshot, swing: bool = True,
                   max_range_nm: float = 2.0) -> ObstacleField:
    """Clearance from every cell to the nearest ship envelope, in NM.

    Negative inside an envelope. Cells further than `max_range_nm` from every
    ship read `max_range_nm`. Computed per ship in a local window, so the cost
    is O(ships x window) rather than O(ships x cells).
    """
    clear = np.full(grid.shape, float(max_range_nm))
    nearest = np.full(grid.shape, -1, dtype=np.int32)
    env = np.array([s.envelope(swing) for s in snap.ships], float).reshape(-1, 3)
    res = grid.resolution_deg
    for i, (la, lo, r) in enumerate(env):
        reach = (r + max_range_nm) / 60.0
        y0 = max(int((la - reach - grid.lats[0]) / res), 0)
        y1 = min(int((la + reach - grid.lats[0]) / res) + 2, grid.ny)
        x0 = max(int((lo - reach / math.cos(math.radians(la)) - grid.lons[0]) / res), 0)
        x1 = min(int((lo + reach / math.cos(math.radians(la)) - grid.lons[0]) / res) + 2, grid.nx)
        if y0 >= y1 or x0 >= x1:
            continue
        LA, LO = np.meshgrid(grid.lats[y0:y1], grid.lons[x0:x1], indexing="ij")
        c = haversine_nm(LA, LO, la, lo) - r
        sub = clear[y0:y1, x0:x1]
        upd = c < sub
        sub[upd] = c[upd]
        nearest[y0:y1, x0:x1][upd] = i
    return ObstacleField(clear, nearest, env, swing, max_range_nm)
