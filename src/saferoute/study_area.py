"""Study area: the Singapore Strait between its east and west entrances and the
Singapore container / cargo terminals.

Everything an experiment needs to be reproducible lives here: the bounding box,
the port register, the schematic Traffic Separation Scheme (TSS), the MPA
anchorage and pilot-boarding reference points, and the own-ship classes whose
dimensions drive the physical constraints (draught -> depth, beam/LOA ->
clearance).

Data provenance
---------------
* Anchorage and pilot boarding positions: MPA, *Singapore Port Information
  2025* (updated to 1 Feb 2025), anchorage and boarding-ground tables. MPA lists
  one or more reference positions per anchorage with a charted depth; these are
  positions INSIDE each anchorage, not its boundary polygon. The anchorage
  extents used to place synthetic ships are therefore approximated as discs
  around those points (obstacles.py), clipped to water.
* The TSS geometry is a *schematic* of the published Singapore Strait scheme,
  digitised to roughly 0.5 NM. Fit for algorithm development and relative
  comparison; NOT chart-accurate and never for navigation.
"""
from __future__ import annotations

import json
import os

from dataclasses import asdict, dataclass, replace
from typing import Dict, List, Tuple

# --------------------------------------------------------------------------
# Bounding box (WGS84 degrees). Lat 1.03-1.45 covers every Singapore terminal,
# both strait entrances and the southern anchorages; lon 103.40-104.45 runs
# from the western to the eastern entrance of the strait.
# --------------------------------------------------------------------------
BBOX = dict(lat_min=1.03, lat_max=1.45, lon_min=103.40, lon_max=104.45)
DEFAULT_RESOLUTION_DEG = 0.002          # ~222 m cells


def _dm(deg: int, minutes: float) -> float:
    return deg + minutes / 60.0


@dataclass(frozen=True)
class Port:
    """A Singapore terminal or a strait entrance.

    `lat`/`lon` is the terminal itself; `app_lat`/`app_lon` is the approach
    position in open water that the router connects to. The last few hundred
    metres from the approach to the berth are a pilotage-and-tug manoeuvre
    inside a basin, below the resolution of this model, and are out of scope.
    Every reported distance is approach-to-approach and says so.
    """
    code: str
    name: str
    lat: float
    lon: float
    app_lat: float
    app_lon: float
    kind: str = "terminal"          # terminal | gateway
    max_draught_m: float = 16.0
    side: str = ""                  # gateways only: "E" or "W"
    berth_name: str = ""
    quay_hdg: float = float("nan")  # direction of the quay line, deg true
    pilot: Tuple[Tuple[float, float], ...] = ()   # pilotage track: board -> berth

    @property
    def approach(self) -> Tuple[float, float]:
        return (self.app_lat, self.app_lon)

    @property
    def berth(self) -> Tuple[float, float]:
        return (self.lat, self.lon)

    def as_dict(self):
        return asdict(self)


PORTS: Dict[str, Port] = {p.code: p for p in [
    # Terminal positions are the quay; approach positions are open water at
    # least ~0.25 NM off the OpenStreetMap shoreline. `max_draught_m` is the
    # terminal's maintained (dredged) depth, applied as a dredged pocket around
    # the approach (seagrid.DREDGED_POCKET_NM).
    Port("SGTUA", "Tuas Port",               1.2500, 103.6260, 1.2450, 103.5980,
         "terminal", 23.0),
    Port("SGJUR", "Jurong Port",             1.3000, 103.7150, 1.2630, 103.7400,
         "terminal", 15.7),
    Port("SGPPT", "Pasir Panjang Terminal",  1.2733, 103.7550, 1.2380, 103.7790,
         "terminal", 16.0),
    Port("SGKEP", "Keppel / Brani Terminal", 1.2640, 103.8380, 1.2460, 103.8620,
         "terminal", 15.0),
    Port("EAST",  "Strait entrance (East)",  1.3200, 104.4300, 1.3200, 104.4300,
         "gateway", 25.0, side="E"),
    Port("WEST",  "Strait entrance (West)",  1.1100, 103.4400, 1.1100, 103.4400,
         # was 1.092, 103.416: GEBCO shows 6-12 m on its own SW edge, so the gateway sits 2.7 NM in
         "gateway", 25.0, side="W"),
]}

# Berths and pilotage tracks (scripts/build_berths.py -> data/berths.json):
# each terminal's route ends ON a berth - a point 40 m off the quay line of
# the modelled coastline - reached by a pilotage track from the nearest water
# the route-planning board can use. When the file is present the terminal's
# position becomes the berth and its approach becomes the start of that track.
_BERTHS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "berths.json")
if os.path.exists(_BERTHS):
    for _code, _b in json.load(open(_BERTHS)).items():
        PORTS[_code] = replace(PORTS[_code], lat=_b["berth"][0], lon=_b["berth"][1],
                               app_lat=_b["pilot"][0][0], app_lon=_b["pilot"][0][1],
                               berth_name=_b["name"], quay_hdg=_b["quay_hdg"],
                               pilot=tuple(tuple(x) for x in _b["pilot"]))

TERMINALS = [c for c, p in PORTS.items() if p.kind == "terminal"]


def fairway(code: str) -> List[Tuple[float, float]]:
    """A terminal's fairway: straight from its approach (the start of the
    pilotage track) to the nearest point of the TSS centreline. Port fairways
    are dredged and kept free of anchored ships; the board uses this to dredge
    the corridor (seagrid) and the synthetic snapshot keeps ships out of it."""
    import math
    p = PORTS[code]
    best, bp = float("inf"), None
    for (a_la, a_lo), (b_la, b_lo) in zip(TSS_CENTRELINE[:-1], TSS_CENTRELINE[1:]):
        for k in range(41):
            t = k / 40
            la, lo = a_la + (b_la - a_la) * t, a_lo + (b_lo - a_lo) * t
            d = math.hypot(la - p.app_lat, (lo - p.app_lon) * math.cos(math.radians(la)))
            if d < best:
                best, bp = d, (la, lo)
    return [p.approach, bp]
GATEWAYS = [c for c, p in PORTS.items() if p.kind == "gateway"]

# Every question the project asks: in from either entrance to every terminal,
# and out from every terminal to either entrance.
OD_PAIRS: List[Tuple[str, str]] = (
    [(g, t) for g in GATEWAYS for t in TERMINALS] +
    [(t, g) for t in TERMINALS for g in GATEWAYS])


# --------------------------------------------------------------------------
# MPA anchorages (Singapore Port Information 2025). One row per reference
# position: (code, name, charted depth m, lat, lon). Purpose drives the mix of
# ship types placed there by the synthetic snapshot.
# --------------------------------------------------------------------------
ANCHORAGE_POINTS: List[Tuple[str, str, float, float, float]] = [
    ("ACBTH", "Changi Barge Temporary Holding", 5.0,  _dm(1, 20.670), _dm(104, 2.989)),
    ("ACGP",  "Changi General Purposes",       18.8,  _dm(1, 19.123), _dm(104, 4.205)),
    ("ACGP",  "Changi General Purposes",       11.8,  _dm(1, 19.995), _dm(104, 3.155)),
    ("AMOW",  "Man-of-War",                    14.0,  _dm(1, 18.558), _dm(104, 4.422)),
    ("AEBA",  "Eastern Bunkering A",           17.5,  _dm(1, 18.346), _dm(104, 3.496)),
    ("AEBB",  "Eastern Bunkering B",           29.7,  _dm(1, 16.947), _dm(103, 59.850)),
    ("AEPBC", "Eastern Petroleum C",           24.7,  _dm(1, 17.953), _dm(103, 58.980)),
    ("ASCB",  "Small Craft B",                 19.4,  _dm(1, 18.262), _dm(103, 59.883)),
    ("ASCA",  "Small Craft A",                 12.4,  _dm(1, 18.235), _dm(103, 57.321)),
    ("AEPBB", "Eastern Petroleum B",           16.3,  _dm(1, 17.953), _dm(103, 56.455)),
    ("AESPA", "Eastern Special Purposes A",    15.7,  _dm(1, 16.785), _dm(103, 55.133)),
    ("AEBC",  "Eastern Bunkering C",           23.0,  _dm(1, 16.593), _dm(103, 55.581)),
    ("AEHA",  "Eastern Holding A",             40.0,  _dm(1, 14.865), _dm(103, 55.347)),
    ("AEPA",  "Eastern Petroleum A",           21.5,  _dm(1, 15.557), _dm(103, 54.083)),
    ("AEW",   "Eastern",                        7.2,  _dm(1, 15.308), _dm(103, 52.341)),
    ("AEW",   "Eastern",                       10.6,  _dm(1, 15.760), _dm(103, 53.315)),
    ("AEW",   "Eastern",                       13.0,  _dm(1, 15.629), _dm(103, 53.790)),
    ("AEHB",  "Eastern Holding B",             18.1,  _dm(1, 14.745), _dm(103, 52.818)),
    ("AEHC",  "Eastern Holding C",              6.9,  _dm(1, 14.973), _dm(103, 51.950)),
    ("AWQI",  "Western Quarantine & Immigration", 11.2, _dm(1, 12.988), _dm(103, 49.817)),
    ("AWW",   "Western",                       15.6,  _dm(1, 15.141), _dm(103, 48.289)),
    ("AWPA",  "Western Petroleum A",           21.5,  _dm(1, 14.293), _dm(103, 48.221)),
    ("AWH",   "Western Holding",               21.9,  _dm(1, 14.048), _dm(103, 47.377)),
    ("AWPB",  "Western Petroleum B",           19.2,  _dm(1, 14.032), _dm(103, 48.393)),
    ("ARAFR", "Raffles Reserved",              11.7,  _dm(1, 11.622), _dm(103, 45.276)),
    ("ARP",   "Raffles Petroleum",             11.3,  _dm(1, 12.167), _dm(103, 44.394)),
    ("ASPLU", "Selat Pauh",                    10.3,  _dm(1, 12.870), _dm(103, 43.596)),
    ("ASPLU", "Selat Pauh",                    10.3,  _dm(1, 13.445), _dm(103, 43.892)),
    ("ASPP",  "Selat Pauh Petroleum",           7.0,  _dm(1, 13.191), _dm(103, 42.965)),
    ("ASPP",  "Selat Pauh Petroleum",           7.0,  _dm(1, 13.096), _dm(103, 43.000)),
    ("ASPH",  "Sudong Petroleum Holding",      21.6,  _dm(1, 9.917),  _dm(103, 43.112)),
    ("ASUEX", "Sudong Explosive",              19.5,  _dm(1, 10.278), _dm(103, 42.685)),
    ("ASSPU", "Sudong Special Purpose",        17.7,  _dm(1, 11.040), _dm(103, 42.434)),
    ("ASH",   "Sudong Holding",                23.2,  _dm(1, 12.051), _dm(103, 39.978)),
    ("AVLCC", "Very Large Crude Carrier",      22.6,  _dm(1, 13.499), _dm(103, 39.125)),
    ("AVLCC", "Very Large Crude Carrier",      22.6,  _dm(1, 13.611), _dm(103, 39.185)),
]

PILOT_BOARDING: List[Tuple[str, str, float, float]] = [
    ("PEBGA", "Eastern Boarding Ground A", _dm(1, 13.517), _dm(103, 53.447)),
    ("PEBGB", "Eastern Boarding Ground B", _dm(1, 15.408), _dm(103, 56.956)),
    ("PEBGC", "Eastern Boarding Ground C", _dm(1, 15.885), _dm(103, 57.834)),
    ("PSBG",  "Southern Boarding Ground",  _dm(1, 11.702), _dm(103, 49.666)),
    ("PWBGA", "Western Boarding Ground A", _dm(1, 12.906), _dm(103, 36.073)),
    ("PWBGB", "Western Boarding Ground B", _dm(1, 12.027), _dm(103, 39.481)),
    ("PJSB",  "East Johor Strait Boarding Ground", _dm(1, 17.650), _dm(104, 6.393)),
    ("PGBG",  "Gusong Boarding Ground",    _dm(1, 10.465), _dm(103, 46.887)),
]

# What each anchorage is for, by code prefix -> weights over ship types. Used
# only by the synthetic snapshot; a real AIS snapshot carries its own types.
ANCHORAGE_USE = {
    "petroleum": ("AEPA", "AEPBB", "AEPBC", "AWPA", "AWPB", "ARP", "ASPP", "ASPH",
                  "AVLCC"),
    "bunkering": ("AEBA", "AEBB", "AEBC"),
    "holding":   ("AEHA", "AEHB", "AEHC", "AWH", "ASH"),
    "general":   ("AEW", "AWW", "ASPLU", "ACGP", "AWQI", "ARAFR", "AESPA",
                  "ASSPU", "ASUEX", "AMOW"),
    "small":     ("ASCA", "ASCB", "ACBTH"),
}


def anchorage_use(code: str) -> str:
    for use, codes in ANCHORAGE_USE.items():
        if code in codes:
            return use
    return "general"


# --------------------------------------------------------------------------
# Traffic Separation Scheme - schematic separation-zone centreline, W -> E
# --------------------------------------------------------------------------
TSS_CENTRELINE: List[Tuple[float, float]] = [
    # Adjusted so the separation line keeps >= 1.3 NM from the OpenStreetMap
    # coastline everywhere; in particular it passes south of Raffles
    # Lighthouse (Pulau Satumu, 1.160 N 103.741 E) as the real scheme does.
    (1.0750, 103.4200),
    (1.1000, 103.5400),
    (1.1180, 103.6400),
    (1.1240, 103.7000),
    (1.1300, 103.7400),
    (1.1580, 103.8000),
    (1.1840, 103.8600),
    (1.2120, 103.9200),
    (1.2400, 104.0100),
    (1.2700, 104.0900),
    (1.2900, 104.1800),
    (1.3050, 104.2800),
    (1.3150, 104.4000),
]
TSS_SEPARATION_HALF_WIDTH_NM = 0.35   # separation zone: no through traffic
TSS_LANE_WIDTH_NM = 1.30              # width of each one-way lane
TSS_INSHORE_BAND_NM = 5.00            # band either side of the lanes (zone 4): no
#   through-transit outside the lanes, and never against the adjacent lane's flow
TSS_NORTH_LANE_HEADING = "W"          # north lane = westbound
TSS_SOUTH_LANE_HEADING = "E"          # south lane = eastbound


# --------------------------------------------------------------------------
# Safe passing distances (CPA) by navigational zone - the project's working
# table. The router keeps OUTSIDE the lower bound as a hard limit and prefers to
# keep outside the upper bound (soft penalty in the "safest" mode).
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CpaRule:
    zone: str
    context: str
    hard_nm: float          # never closer than this (edge removed)
    target_nm: float        # preferred passing distance (penalised inside)


CPA_RULES: Dict[str, CpaRule] = {
    "tss": CpaRule("Singapore Strait TSS",
                   "high-density transit in the lanes (Main Strait, Phillip Channel)",
                   0.30, 1.00),
    "approach": CpaRule("Port approaches & fairways",
                        "Jurong Island, Sinki Fairway, Keppel Harbour approaches",
                        0.20, 0.50),
    "basin": CpaRule("Berthing basins & narrow channels",
                     "slow-speed transit with tugs and pilots",
                     0.05, 0.22),   # ~1-2 ship lengths
}


# --------------------------------------------------------------------------
# Own-ship classes. Only physical dimensions matter here: draught sets the
# depth limit, beam and LOA set clearances. Service speed is kept solely to
# turn a distance into an indicative passage time and to animate the 3D wake.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class VesselClass:
    code: str
    name: str
    loa_m: float
    beam_m: float
    draught_m: float
    moulded_depth_m: float
    service_speed_kn: float
    teu: float = 0.0
    hull_type: str = "container"     # container | tanker | bulker

    def as_dict(self):
        return asdict(self)


VESSEL_CLASSES: Dict[str, VesselClass] = {v.code: v for v in [
    VesselClass("FEEDER",  "Feeder container (1,700 TEU)",   180, 28, 9.5,  14.5, 12.0, 1_700),
    VesselClass("PANAMAX", "Panamax container (4,500 TEU)",  294, 32, 12.5, 21.5, 12.0, 4_500),
    VesselClass("NPX",     "Neo-Panamax container (13,000 TEU)", 366, 51, 15.2, 29.9, 12.0, 13_000),
    VesselClass("ULCS",    "Ultra-large container (24,000 TEU)", 400, 61, 16.5, 33.2, 12.0, 24_000),
]}
DEFAULT_VESSEL = "PANAMAX"

# Harbour-limit speed used for the indicative passage time. MPA sets speed
# limits inside port waters; 12 kn is a representative planning figure.
PLANNING_SPEED_KN = 12.0


def port(code: str) -> Port:
    try:
        return PORTS[code]
    except KeyError as exc:
        raise KeyError(f"unknown port code {code!r}; known: {sorted(PORTS)}") from exc


def vessel(code: str) -> VesselClass:
    try:
        return VESSEL_CLASSES[code]
    except KeyError as exc:
        raise KeyError(f"unknown vessel class {code!r}; "
                       f"known: {sorted(VESSEL_CLASSES)}") from exc
