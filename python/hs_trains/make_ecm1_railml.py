"""Generate a complete RailML 3.3 file for a train run on ECM1 (East Coast Main Line).

Produces both ``<infrastructure>`` and ``<timetable>`` sections in a single file.
The infrastructure covers all ECM1 segments; the timetable defines one train
(OT_ECM1_Real) whose route is the shortest BFS path through the full ECM1
graph from the southernmost to the northernmost open end.

Because ECM1 has many parallel tracks and junctions, this BFS path naturally
passes through Y-junction nodes (valancy = 3) and diamond crossing nodes
(valancy = 4).

Usage
-----
    uv run make-ecm1-railml ecm1_network.xml
"""

import argparse
import xml.etree.ElementTree as ET
from collections import deque, defaultdict
from itertools import combinations
from pathlib import Path
from xml.etree.ElementTree import ElementTree, fromstring, indent

import geopandas as gpd

from hs_trains.gtcl import (
    GPKG,
    build_functional_nodes,
    build_net_elements,
    build_net_relations,
    build_networks,
    build_tracks,
    round_bng,
)
from hs_trains.model.infrastructure import (
    FunctionalInfrastructure,
    Infrastructure,
    RailML,
    Topology,
)

_NS_URI = "https://www.railml.org/schemas/3.3"
_NS = f"{{{_NS_URI}}}"


def _bfs_parents(
    start: str, adj: dict[str, list[str]]
) -> dict[str, str | None]:
    """BFS from `start`; returns a parent map for path reconstruction."""
    parent: dict[str, str | None] = {start: None}
    queue: deque[str] = deque([start])
    while queue:
        cur = queue.popleft()
        for nxt in adj[cur]:
            if nxt not in parent:
                parent[nxt] = cur
                queue.append(nxt)
    return parent


def _reconstruct(goal: str, parent: dict[str, str | None]) -> list[str]:
    path: list[str] = []
    cur: str | None = goal
    while cur is not None:
        path.append(cur)
        cur = parent.get(cur)
    path.reverse()
    return path


def _find_route(lines_bng: gpd.GeoDataFrame) -> list[str]:
    """Find a south-to-north route through ECM1 that passes through at least one
    Y-junction and one diamond crossing.

    Strategy:
    1. Build the full ECM1 segment adjacency graph from shared endpoints.
    2. BFS from south open-end and from north open-end to get distance/parent maps.
    3. For each degree-4 (diamond crossing) coordinate, find the pair of segments
       (s_in from the south side, s_out toward the north side) that minimises total
       hops.  Reconstruct the two half-paths and join them at the crossing.
    4. Fall back to simple south-to-north BFS if no crossing is reachable.

    Returns an ordered list of ASSETID strings.
    """
    # Build endpoint → segment adjacency (1 m BNG precision, matching net relations).
    endpoint_map: dict[tuple[int, int], list[str]] = defaultdict(list)
    for _, row in lines_bng.iterrows():
        seg_id = str(row["ASSETID"])
        coords = list(row.geometry.coords)
        endpoint_map[round_bng(*coords[0])].append(seg_id)
        endpoint_map[round_bng(*coords[-1])].append(seg_id)

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

    parent_s = _bfs_parents(start_seg, adj)
    parent_n = _bfs_parents(goal_seg, adj)
    dist_s = {seg: 0 for seg in parent_s}  # present = reachable; we only need reachability
    dist_n = {seg: 0 for seg in parent_n}

    # Compute actual hop distances for the crossing search.
    def _distances(start: str) -> dict[str, int]:
        d: dict[str, int] = {start: 0}
        q: deque[str] = deque([start])
        while q:
            cur = q.popleft()
            for nxt in adj[cur]:
                if nxt not in d:
                    d[nxt] = d[cur] + 1
                    q.append(nxt)
        return d

    dist_from_south = _distances(start_seg)
    dist_from_north = _distances(goal_seg)

    # Find the diamond crossing that adds fewest extra hops.
    best_crossing: tuple[int, str, str] | None = None  # (total_hops, s_in, s_out)
    for coord, segs in endpoint_map.items():
        if len(segs) != 4:
            continue
        for s_in in segs:
            if s_in not in dist_from_south:
                continue
            for s_out in segs:
                if s_out == s_in or s_out not in dist_from_north:
                    continue
                total = dist_from_south[s_in] + 1 + dist_from_north[s_out]
                if best_crossing is None or total < best_crossing[0]:
                    best_crossing = (total, s_in, s_out)

    if best_crossing is not None:
        _, s_in, s_out = best_crossing
        south_half = _reconstruct(s_in, parent_s)
        north_half = _reconstruct(s_out, parent_n)
        north_half.reverse()
        # Deduplicate at the join: south_half ends with s_in, north_half starts with s_out.
        route = south_half + north_half
    else:
        # No diamond crossing reachable from both ends — fall back to direct path.
        print("  NOTE: no diamond crossing reachable from both ends; using direct path")
        if goal_seg not in parent_s:
            raise SystemExit("BFS could not connect south to north of ECM1")
        route = _reconstruct(goal_seg, parent_s)

    # Deduplicate while preserving order (in case BFS half-paths share a prefix).
    seen: set[str] = set()
    unique_route: list[str] = []
    for seg in route:
        if seg not in seen:
            seen.add(seg)
            unique_route.append(seg)
    route = unique_route

    # Build a coord lookup for reporting.
    assetid_coords: dict[str, list[tuple[int, int]]] = {}
    for _, row in lines_bng.iterrows():
        coords = list(row.geometry.coords)
        assetid_coords[str(row["ASSETID"])] = [
            round_bng(*coords[0]),
            round_bng(*coords[-1]),
        ]
    route_coords: set[tuple[int, int]] = set()
    for assetid in route:
        for c in assetid_coords.get(assetid, []):
            route_coords.add(c)

    junction_count = sum(1 for c in route_coords if len(endpoint_map.get(c, [])) == 3)
    crossing_count = sum(1 for c in route_coords if len(endpoint_map.get(c, [])) == 4)
    print(f"  Route segments   : {len(route)}")
    print(f"  Y-junctions      : {junction_count}")
    print(f"  Diamond crossings: {crossing_count}")
    if junction_count == 0:
        print("  WARNING: no Y-junction found on this path")
    if crossing_count == 0:
        print("  WARNING: no diamond crossing found on this path")

    return route


def _build_timetable(route: list[str]) -> ET.Element:
    """Build the <rail3:timetable> element for the ECM1 route."""
    tt = ET.Element(f"{_NS}timetable")

    # baseItineraries
    base_itis = ET.SubElement(tt, f"{_NS}baseItineraries")
    bit = ET.SubElement(base_itis, f"{_NS}baseItinerary", {"id": "BIT_ECM1_Real"})

    bip_south = ET.SubElement(
        bit,
        f"{_NS}baseItineraryPoint",
        {
            "id": "BIP_ECM1_South",
            "sequenceNumber": "1",
            "locationRef": "OP_ECM1_South",
        },
    )
    followup_secs = ET.SubElement(bip_south, f"{_NS}followupSections")
    followup_sec = ET.SubElement(followup_secs, f"{_NS}followupSection")
    track_refs = ET.SubElement(followup_sec, f"{_NS}trackRefs")
    for seq, assetid in enumerate(route, start=1):
        ET.SubElement(
            track_refs,
            f"{_NS}trackRef",
            {"ref": f"track_{assetid}", "sequenceNumber": str(seq)},
        )

    ET.SubElement(
        bit,
        f"{_NS}baseItineraryPoint",
        {
            "id": "BIP_ECM1_North",
            "sequenceNumber": "2",
            "locationRef": "OP_ECM1_North",
        },
    )

    # itineraries
    itis = ET.SubElement(tt, f"{_NS}itineraries")
    iti = ET.SubElement(itis, f"{_NS}itinerary", {"id": "ITI_ECM1_Real"})
    ET.SubElement(
        iti,
        f"{_NS}range",
        {"baseItineraryRef": "BIT_ECM1_Real", "sequenceNumber": "1"},
    )

    # operationalTrains
    op_trains = ET.SubElement(tt, f"{_NS}operationalTrains")
    op_train = ET.SubElement(
        op_trains, f"{_NS}operationalTrain", {"id": "OT_ECM1_Real"}
    )
    ET.SubElement(
        op_train,
        f"{_NS}operationalTrainVariant",
        {
            "id": "OTV_ECM1_Real",
            "itineraryRef": "ITI_ECM1_Real",
            "validityRef": "V1",
        },
    )

    return tt


def _inject_operational_points(infra_root: ET.Element) -> None:
    """Add stub operationalPoints for the south and north route terminals.

    The Rust parser stores these in the ops map for informational use only.
    """
    fi = infra_root.find(f"{_NS}functionalInfrastructure")
    if fi is None:
        fi = ET.SubElement(infra_root, f"{_NS}functionalInfrastructure")

    ops_el = ET.SubElement(fi, f"{_NS}operationalPoints")
    ET.SubElement(
        ops_el,
        f"{_NS}operationalPoint",
        {"id": "OP_ECM1_South", "name": "ECM1 south terminal (London area)"},
    )
    ET.SubElement(
        ops_el,
        f"{_NS}operationalPoint",
        {"id": "OP_ECM1_North", "name": "ECM1 north terminal (Doncaster area)"},
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a complete RailML 3.3 file for a train on ECM1."
    )
    parser.add_argument(
        "output",
        nargs="?",
        default="examples/ecm1/ecm1_network.xml",
        help="Output XML file path (default: examples/ecm1/ecm1_network.xml)",
    )
    args = parser.parse_args()

    if not GPKG.exists():
        raise SystemExit(f"GeoPackage not found: {GPKG}")

    # ------------------------------------------------------------------
    # Load ECM1 segments
    # ------------------------------------------------------------------
    print(f"Loading ECM1 from {GPKG} …")
    lines_bng: gpd.GeoDataFrame = gpd.read_file(GPKG, layer="NWR_GTCL")
    lines_bng = lines_bng[
        (lines_bng["SUPERCEDED"] != "YES") & (lines_bng["ELR"] == "ECM1")
    ].reset_index(drop=True)
    if lines_bng.empty:
        raise SystemExit("No ECM1 segments found in GeoPackage")

    print("Loading NWR_GTCL_Nodes …")
    nodes_bng: gpd.GeoDataFrame = gpd.read_file(GPKG, layer="NWR_GTCL_Nodes")
    nodes_bng = nodes_bng[nodes_bng["SUPERCEDED"] != "YES"].reset_index(drop=True)

    lines_wgs84 = lines_bng.to_crs("EPSG:4326")
    nodes_wgs84 = nodes_bng.to_crs("EPSG:4326")

    print(f"  {len(lines_bng):,} ECM1 segments, {len(nodes_bng):,} nodes (all ELRs)")

    # ------------------------------------------------------------------
    # Find route
    # ------------------------------------------------------------------
    print("Finding BFS route through ECM1 …")
    route = _find_route(lines_bng)

    seg_length: dict[str, float] = {
        str(row["ASSETID"]): row.geometry.length for _, row in lines_bng.iterrows()
    }
    total_length_m = sum(seg_length.get(a, 0.0) for a in route)
    print(f"  Total route length: {total_length_m:,.0f} m  ({total_length_m/1000:.1f} km)")

    # ------------------------------------------------------------------
    # Build infrastructure
    # ------------------------------------------------------------------
    print("Building net elements …")
    net_elements = build_net_elements(lines_bng)

    print("Building net relations …")
    net_relations, endpoints = build_net_relations(lines_bng)

    print("Building networks …")
    networks = build_networks(lines_bng)

    print("Building tracks …")
    tracks = build_tracks(lines_bng, lines_wgs84)

    print("Building functional nodes …")
    switches, buffer_stops, borders, crossings = build_functional_nodes(
        nodes_bng, nodes_wgs84, endpoints
    )
    print(f"  switches={len(switches)}, crossings={len(crossings)}, buffer_stops={len(buffer_stops)}")

    # ------------------------------------------------------------------
    # Serialise infrastructure via pydantic model
    # ------------------------------------------------------------------
    print("Assembling RailML …")
    ET.register_namespace("rail3", _NS_URI)
    ET.register_namespace("gml", "http://www.opengis.net/gml/3.2")

    railml_infra = RailML(
        infrastructure=Infrastructure(
            topology=Topology(
                net_elements=net_elements,
                net_relations=net_relations,
                networks=networks,
            ),
            functional_infrastructure=FunctionalInfrastructure(
                tracks=tracks,
                switches=switches,
                crossings=crossings,
                buffer_stops=buffer_stops,
                borders=borders,
            ),
        )
    )

    xml_str = railml_infra.to_xml(encoding="unicode", exclude_none=True)
    root = fromstring(xml_str)

    # Inject the two stub operational points that the timetable locationRefs point to.
    infra_el = root.find(f"{_NS}infrastructure")
    if infra_el is not None:
        _inject_operational_points(infra_el)

    # ------------------------------------------------------------------
    # Build timetable and attach to root
    # ------------------------------------------------------------------
    root.append(_build_timetable(route))

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    indent(root, space="  ")
    output = Path(args.output)
    ElementTree(root).write(str(output), encoding="unicode", xml_declaration=True)

    size_mb = output.stat().st_size / 1_000_000
    print(f"\nWrote {output}  ({size_mb:.1f} MB)")
    print(f"  netElements  : {len(net_elements):,}")
    print(f"  tracks       : {len(tracks):,}")
    print(f"  route tracks : {len(route)}")


if __name__ == "__main__":
    main()
