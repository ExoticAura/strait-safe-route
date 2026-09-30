"""Static pathfinding.

Dijkstra (1959) and A* (Hart, Nilsson & Raphael, 1968) over the CSR graph
built in network.py, both instrumented identically so the comparison in the algorithm comparison
is like-for-like: same graph, same tie-breaking, same data structures, same
language.  The only difference between the two implementations is the
`h` term, which is exactly what the research question is about.

Admissibility
-------------
The heuristic is the great-circle distance from a node to the goal.  Every
edge satisfies c(u, v) >= d(u, v), and great-circle distance obeys the
triangle inequality, so

    h(u) = d(u, goal) <= sum of d along any u->goal path <= cost of that path

making h admissible; and h(u) - h(v) <= d(u, v) <= c(u, v) makes it
consistent, so A* never needs to re-expand a closed node.  `test_algorithms`
asserts the two algorithms agree on cost for every test pair, which is the
practical check that this holds in the built graph.
"""
from __future__ import annotations

import heapq
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy import ndimage

from .geo import haversine_nm
from .network import NavNetwork

INF = float("inf")


@dataclass
class SearchResult:
    algorithm: str
    path: List[int]
    cost: float                  # effective NM (the objective actually minimised)
    found: bool
    nodes_expanded: int
    edges_relaxed: int
    max_frontier: int
    runtime_s: float
    epsilon: float = 1.0

    def as_row(self) -> Dict[str, object]:
        return dict(algorithm=self.algorithm, epsilon=self.epsilon,
                    found=self.found, cost_effective_nm=self.cost,
                    nodes_expanded=self.nodes_expanded,
                    edges_relaxed=self.edges_relaxed,
                    max_frontier=self.max_frontier,
                    runtime_ms=self.runtime_s * 1000.0,
                    path_nodes=len(self.path))


def _reconstruct(parent: np.ndarray, src: int, dst: int) -> List[int]:
    path, cur = [dst], dst
    while cur != src:
        cur = int(parent[cur])
        if cur < 0:
            return []
        path.append(cur)
    path.reverse()
    return path


# --------------------------------------------------------------------------
def dijkstra(net: NavNetwork, src: int, dst: int) -> SearchResult:
    """Uniform-cost search. Expands in order of g, so h is identically zero."""
    t0 = time.perf_counter()
    n = net.n_nodes
    g = np.full(n, INF)
    parent = np.full(n, -1, dtype=np.int64)
    closed = np.zeros(n, dtype=bool)
    indptr, indices, cost = net.indptr, net.indices, net.cost

    g[src] = 0.0
    heap = [(0.0, src)]
    expanded = relaxed = 0
    max_frontier = 1

    while heap:
        max_frontier = max(max_frontier, len(heap))
        gu, u = heapq.heappop(heap)
        if closed[u]:
            continue
        closed[u] = True
        expanded += 1
        if u == dst:
            break
        for k in range(indptr[u], indptr[u + 1]):
            v = indices[k]
            relaxed += 1
            nd = gu + cost[k]
            if nd < g[v]:
                g[v] = nd
                parent[v] = u
                heapq.heappush(heap, (nd, int(v)))

    found = closed[dst]
    return SearchResult("dijkstra", _reconstruct(parent, src, dst) if found else [],
                        float(g[dst]) if found else INF, bool(found),
                        expanded, relaxed, max_frontier,
                        time.perf_counter() - t0)


# --------------------------------------------------------------------------
def astar(net: NavNetwork, src: int, dst: int, epsilon: float = 1.0
          ) -> SearchResult:
    """A* with the great-circle-distance heuristic.

    `epsilon` > 1 gives weighted A*: faster, and the returned cost is
    guaranteed within a factor `epsilon` of optimal.  epsilon = 1 is exact.
    """
    t0 = time.perf_counter()
    n = net.n_nodes
    g = np.full(n, INF)
    parent = np.full(n, -1, dtype=np.int64)
    closed = np.zeros(n, dtype=bool)
    indptr, indices, cost = net.indptr, net.indices, net.cost

    # Vectorised heuristic: one haversine pass over all nodes up front is far
    # cheaper than a Python-level call per pop, and it is the same value.
    h = haversine_nm(net.node_lat, net.node_lon,
                     net.node_lat[dst], net.node_lon[dst])

    g[src] = 0.0
    heap = [(epsilon * float(h[src]), src)]
    expanded = relaxed = 0
    max_frontier = 1

    while heap:
        max_frontier = max(max_frontier, len(heap))
        _, u = heapq.heappop(heap)
        if closed[u]:
            continue
        closed[u] = True
        expanded += 1
        if u == dst:
            break
        gu = g[u]
        for k in range(indptr[u], indptr[u + 1]):
            v = indices[k]
            relaxed += 1
            nd = gu + cost[k]
            if nd < g[v]:
                g[v] = nd
                parent[v] = u
                heapq.heappush(heap, (nd + epsilon * float(h[v]), int(v)))

    found = closed[dst]
    name = "astar" if epsilon == 1.0 else f"astar_w{epsilon:g}"
    return SearchResult(name, _reconstruct(parent, src, dst) if found else [],
                        float(g[dst]) if found else INF, bool(found),
                        expanded, relaxed, max_frontier,
                        time.perf_counter() - t0, epsilon=epsilon)


# --------------------------------------------------------------------------
def k_shortest_paths(net: NavNetwork, src: int, dst: int, k: int = 3,
                     diversity_nm: Optional[float] = None,
                     diversity_fraction: float = 0.04) -> List[SearchResult]:
    """Spatially distinct alternatives, for presenting route options.

    Yen's algorithm produces near-duplicates on a dense grid - alternatives
    that differ by one cell.  Instead each successive route is found after
    penalising a corridor of width `diversity_nm` around the routes already
    returned, which yields alternatives a watchkeeper would recognise as
    different (north of the islands vs south of them), at the price of losing
    the optimality guarantee for routes 2..k.  Those are labelled as
    alternatives, never as optimal.

    When `diversity_nm` is not given it is set to `diversity_fraction` of the
    straight-line distance, with a floor of 1 NM.
    """
    if diversity_nm is None:
        gc = float(haversine_nm(net.node_lat[src], net.node_lon[src],
                                net.node_lat[dst], net.node_lon[dst]))
        diversity_nm = max(1.0, diversity_fraction * gc)
    import copy
    results: List[SearchResult] = []
    penalised = net.cost.copy()
    original = net.cost

    try:
        for i in range(k):
            net.cost = penalised
            r = astar(net, src, dst)
            if not r.found:
                break
            r.algorithm = "astar" if i == 0 else f"alternative_{i}"
            # Recompute the honest cost of this path on the unpenalised graph.
            net.cost = original
            r.cost = net.path_cost(r.path)[0]
            results.append(r)

            # Penalise a corridor around the route just taken: rasterise the
            # route and run one distance transform, rather than N x V haversines.
            grid = net.grid
            mark = np.zeros(grid.shape, dtype=bool)
            mark[net.node_iy[r.path], net.node_ix[r.path]] = True
            edt = ndimage.distance_transform_edt(~mark)

            cell_nm = grid.resolution_deg * 60.0
            width_cells = max(1.0, diversity_nm / cell_nm)
            mind = edt[net.node_iy, net.node_ix] / width_cells   # 0 on the route
            factor = np.where(mind < 1.0, 1.0 + 0.80 * (1.0 - mind), 1.0)
            edge_factor = np.repeat(factor, np.diff(net.indptr))
            penalised = penalised * edge_factor
    finally:
        net.cost = original
    return results


# --------------------------------------------------------------------------
def compare_algorithms(net: NavNetwork, src: int, dst: int,
                       epsilons: Sequence[float] = (1.0, 1.15, 1.35)
                       ) -> List[SearchResult]:
    out = [dijkstra(net, src, dst)]
    for e in epsilons:
        out.append(astar(net, src, dst, epsilon=e))
    return out
