"""Shared helpers for loading and converting the NWR_GTCL GeoPackage to RailML.

These functions are used by both ``make_gtcl_railml`` (full-network export) and
``make_ecm1_railml`` (single-line export with timetable).
"""

import uuid
from collections import defaultdict, deque
from decimal import Decimal
from itertools import combinations
from pathlib import Path
from typing import Literal

import geopandas as gpd

from hs_trains.model.infrastructure import (
    Border,
    BufferStop,
    CrossingIS,
    ElementA,
    ElementB,
    ElementCollectionUnordered,
    FunctionalInfrastructure,
    GmlLineString,
    GmlLocation,
    GmlPoint,
    GmlPosList,
    GmlPos,
    Infrastructure,
    NetElement,
    NetElementRef,
    NetRelation,
    Network,
    NetworkResource,
    RailML,
    SwitchIS,
    Topology,
    Track,
    Length,
)
from hs_trains.model.common import Designator, Name

GPKG = Path(__file__).parents[2] / "assets" / "NWR_GTCL20260309.gpkg"

# Coordinate rounding precision (metres in BNG) used to match segment endpoints.
# 1 m is generous enough to absorb floating-point noise while avoiding false matches.
_MATCH_PRECISION = 0


def round_bng(x: float, y: float) -> tuple[int, int]:
    return (round(x, _MATCH_PRECISION), round(y, _MATCH_PRECISION))


def poslist(coords: list[tuple[float, float]]) -> str:
    return " ".join(f"{lon:.6f} {lat:.6f}" for lon, lat in coords)


def pospoint(x: float, y: float) -> str:
    return f"{x:.6f} {y:.6f}"


def build_net_elements(lines_bng: gpd.GeoDataFrame) -> list[NetElement]:
    elements = []
    for _, row in lines_bng.iterrows():
        elements.append(
            NetElement(
                id=f"ne_{row['ASSETID']}",
                length=Length(quantity=Decimal(f"{row.geometry.length:.2f}")),
                designators=[
                    Designator(register_name="NR-ELR", entry=str(row["ELR"])),
                    Designator(register_name="NR-ASSETID", entry=str(row["ASSETID"])),
                ],
            )
        )
    return elements


def build_net_relations(
    lines_bng: gpd.GeoDataFrame,
) -> tuple[list[NetRelation], set[tuple[int, int]]]:
    """Infer connectivity from shared segment endpoints.

    For each node coordinate, collect all segment ends that coincide there,
    then emit one NetRelation for every pair.  This is conservative: at a
    switch with three meeting segments it produces three relations (all
    traversal options), because GTCL carries no routing constraint.

    Also returns the set of all endpoint coordinates so callers can avoid a
    redundant pass over the geometry.
    """
    endpoint_map: dict[tuple[int, int], list[tuple[str, Literal["0", "1"]]]] = defaultdict(list)
    for _, row in lines_bng.iterrows():
        seg_id = f"ne_{row['ASSETID']}"
        coords = list(row.geometry.coords)
        endpoint_map[round_bng(*coords[0])].append((seg_id, "0"))
        endpoint_map[round_bng(*coords[-1])].append((seg_id, "1"))

    relations = []
    rel_idx = 0
    for endpoints in endpoint_map.values():
        if len(endpoints) < 2:
            continue
        for (id_a, pos_a), (id_b, pos_b) in combinations(endpoints, 2):
            relations.append(
                NetRelation(
                    id=f"nr_{rel_idx:07d}",
                    navigability="Both",
                    position_on_a=pos_a,
                    position_on_b=pos_b,
                    element_a=ElementA(ref=id_a),
                    element_b=ElementB(ref=id_b),
                )
            )
            rel_idx += 1
    return relations, set(endpoint_map.keys())


def build_networks(lines_bng: gpd.GeoDataFrame) -> list[Network]:
    elr_to_ids: dict[str, list[str]] = defaultdict(list)
    for _, row in lines_bng.iterrows():
        elr_to_ids[str(row["ELR"])].append(f"ne_{row['ASSETID']}")

    return [
        Network(
            id=f"net_{elr}",
            names=[Name(name=elr, language="en")],
            designators=[Designator(register_name="NR-ELR", entry=elr)],
            network_resource=NetworkResource(
                element_collection_unordered=ElementCollectionUnordered(
                    net_element_refs=[NetElementRef(ref=i) for i in ids]
                )
            ),
        )
        for elr, ids in sorted(elr_to_ids.items())
    ]


def build_tracks(
    lines_bng: gpd.GeoDataFrame, lines_wgs84: gpd.GeoDataFrame
) -> list[Track]:
    tracks = []
    for (_, row_bng), (_, row_wgs84) in zip(
        lines_bng.iterrows(), lines_wgs84.iterrows()
    ):
        asset_id = str(row_bng["ASSETID"])
        tracks.append(
            Track(
                id=f"track_{asset_id}",
                net_element_ref=f"ne_{asset_id}",
                designators=[
                    Designator(register_name="NR-ELR", entry=str(row_bng["ELR"])),
                    Designator(register_name="NR-ASSETID", entry=asset_id),
                ],
                gml_locations=[
                    GmlLocation(
                        line_string=GmlLineString(
                            pos_list=GmlPosList(
                                root=poslist(list(row_wgs84.geometry.coords))
                            )
                        )
                    )
                ],
            )
        )
    return tracks


def build_ecm1_adjacency(
    lines_bng: gpd.GeoDataFrame,
) -> dict[tuple[int, int], list[str]]:
    """Build endpoint-coordinate → [ASSETID, …] adjacency for all ECM1 segments.

    Returns the endpoint map used by :func:`find_ecm1_route` and for node
    classification (degree = number of segments sharing a coordinate).
    """
    endpoint_map: dict[tuple[int, int], list[str]] = defaultdict(list)
    for _, row in lines_bng.iterrows():
        seg_id = str(row["ASSETID"])
        coords = list(row.geometry.coords)
        endpoint_map[round_bng(*coords[0])].append(seg_id)
        endpoint_map[round_bng(*coords[-1])].append(seg_id)
    return endpoint_map


def find_ecm1_route(
    lines_bng: gpd.GeoDataFrame,
    endpoint_map: dict[tuple[int, int], list[str]],
) -> list[str]:
    """Find a south-to-north route through ECM1 that passes through at least one
    Y-junction and one diamond crossing.

    Strategy:
    1. Build the full segment adjacency graph from shared endpoints.
    2. BFS from south open-end and from north open-end to get distance/parent maps.
    3. For each degree-4 (diamond crossing) coordinate, find the pair of segments
       (s_in from the south side, s_out toward the north side) that minimises total
       hops.  Reconstruct the two half-paths and join them at the crossing.
    4. Fall back to simple south-to-north BFS if no crossing is reachable.

    Returns an ordered list of ASSETID strings.
    """
    adj: dict[str, list[str]] = defaultdict(list)
    for segs in endpoint_map.values():
        for s1, s2 in combinations(segs, 2):
            adj[s1].append(s2)
            adj[s2].append(s1)

    # Degree-1 nodes are open ends; sort by BNG northing (south first).
    deg1 = sorted(
        [(coord, segs[0]) for coord, segs in endpoint_map.items() if len(segs) == 1],
        key=lambda x: x[0][1],
    )
    if len(deg1) < 2:
        raise SystemExit("ECM1 graph has fewer than 2 open ends — cannot find a route")

    start_seg = deg1[0][1]
    goal_seg = deg1[-1][1]

    def _bfs(start: str) -> tuple[dict[str, str | None], dict[str, int]]:
        parent: dict[str, str | None] = {start: None}
        dist: dict[str, int] = {start: 0}
        q: deque[str] = deque([start])
        while q:
            cur = q.popleft()
            for nxt in adj[cur]:
                if nxt not in parent:
                    parent[nxt] = cur
                    dist[nxt] = dist[cur] + 1
                    q.append(nxt)
        return parent, dist

    def _reconstruct(goal: str, parent: dict[str, str | None]) -> list[str]:
        path: list[str] = []
        cur: str | None = goal
        while cur is not None:
            path.append(cur)
            cur = parent.get(cur)
        path.reverse()
        return path

    parent_s, dist_s = _bfs(start_seg)
    parent_n, dist_n = _bfs(goal_seg)

    # Find the diamond crossing that adds fewest extra hops.
    best: tuple[int, str, str] | None = None  # (total_hops, s_in, s_out)
    for coord, segs in endpoint_map.items():
        if len(segs) != 4:
            continue
        for s_in in segs:
            if s_in not in dist_s:
                continue
            for s_out in segs:
                if s_out == s_in or s_out not in dist_n:
                    continue
                total = dist_s[s_in] + 1 + dist_n[s_out]
                if best is None or total < best[0]:
                    best = (total, s_in, s_out)

    if best is not None:
        _, s_in, s_out = best
        south_half = _reconstruct(s_in, parent_s)
        north_half = _reconstruct(s_out, parent_n)
        north_half.reverse()
        route = south_half + north_half
    else:
        print("  NOTE: no diamond crossing reachable from both ends; using direct path")
        if goal_seg not in parent_s:
            raise SystemExit("BFS could not connect south to north of ECM1")
        route = _reconstruct(goal_seg, parent_s)

    # Deduplicate while preserving order (half-paths may share a prefix).
    seen: set[str] = set()
    unique: list[str] = []
    for seg in route:
        if seg not in seen:
            seen.add(seg)
            unique.append(seg)

    # Build coord lookup for reporting.
    assetid_coords: dict[str, list[tuple[int, int]]] = {}
    for _, row in lines_bng.iterrows():
        coords = list(row.geometry.coords)
        assetid_coords[str(row["ASSETID"])] = [round_bng(*coords[0]), round_bng(*coords[-1])]
    route_coords: set[tuple[int, int]] = set()
    for assetid in unique:
        for c in assetid_coords.get(assetid, []):
            route_coords.add(c)

    junction_count = sum(1 for c in route_coords if len(endpoint_map.get(c, [])) == 3)
    crossing_count = sum(1 for c in route_coords if len(endpoint_map.get(c, [])) == 4)
    print(f"  Route segments   : {len(unique)}")
    print(f"  Y-junctions      : {junction_count}")
    print(f"  Diamond crossings: {crossing_count}")
    if junction_count == 0:
        print("  WARNING: no Y-junction found on this path")
    if crossing_count == 0:
        print("  WARNING: no diamond crossing found on this path")

    return unique


def build_functional_nodes(
    nodes_bng: gpd.GeoDataFrame,
    nodes_wgs84: gpd.GeoDataFrame,
    segment_endpoints: set[tuple[int, int]],
) -> tuple[list[SwitchIS], list[BufferStop], list[Border], list[CrossingIS]]:
    """Only emit functional nodes whose coordinates appear in segment_endpoints.

    This avoids including the entire network's node set when only a subset of
    segments (e.g. a single ELR) has been selected.

    Valancy classification:
      1   → bufferStop   (dead end)
      3   → switchIS     (Y-junction)
      4   → crossingIS   (diamond crossing — two lines cross at grade)
      ≥5  → switchIS     (complex junction, treated as switch)
      2   → through-node; no functional element generated

    Precondition: ``nodes_bng`` and ``nodes_wgs84`` must have identical length
    and row ordering — both should be filtered and ``reset_index``'d consistently
    before calling this function.
    """
    assert len(nodes_bng) == len(nodes_wgs84), (
        "nodes_bng and nodes_wgs84 must have identical length and ordering; "
        "ensure both are filtered and reset_index'd consistently"
    )
    switches: list[SwitchIS] = []
    buffer_stops: list[BufferStop] = []
    borders: list[Border] = []
    crossings: list[CrossingIS] = []

    for (_, row_bng), (_, row_wgs84) in zip(
        nodes_bng.iterrows(), nodes_wgs84.iterrows()
    ):
        coord = round_bng(row_bng.geometry.x, row_bng.geometry.y)
        if coord not in segment_endpoints:
            continue

        valancy = row_bng.get("VALANCY")
        asset_id = str(row_bng.get("ASSETID") or uuid.uuid4())
        lon, lat = row_wgs84.geometry.x, row_wgs84.geometry.y
        gml = [GmlLocation(point=GmlPoint(pos=GmlPos(root=pospoint(lon, lat))))]

        if valancy == 1.0:
            buffer_stops.append(BufferStop(id=f"bs_{asset_id}", gml_locations=gml))
        elif valancy == 3.0:
            switches.append(SwitchIS(id=f"sw_{asset_id}", gml_locations=gml))
        elif valancy == 4.0:
            crossings.append(CrossingIS(id=f"cx_{asset_id}", gml_locations=gml))
        elif valancy is not None and valancy > 4.0:
            # Complex junction (e.g. double slip) — treat as switch
            switches.append(SwitchIS(id=f"sw_{asset_id}", gml_locations=gml))
        # valancy 2 = simple through-node; no functional element needed

    return switches, buffer_stops, borders, crossings
