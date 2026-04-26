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
    uv run make-ecm1-railml
    uv run make-ecm1-railml examples/ecm1/ecm1_network.xml
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from xml.etree.ElementTree import ElementTree, fromstring, indent

import geopandas as gpd
import typer

from hs_trains.gtcl import (
    GPKG,
    build_ecm1_adjacency,
    build_functional_nodes,
    build_net_elements,
    build_net_relations,
    build_networks,
    build_tracks,
    find_ecm1_route,
)
from hs_trains.model.infrastructure import (
    FunctionalInfrastructure,
    Infrastructure,
    RailML,
    Topology,
)

_NS_URI = "https://www.railml.org/schemas/3.3"
_NS = f"{{{_NS_URI}}}"


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


app = typer.Typer()


@app.command()
def main(
    output: Path = typer.Argument(
        Path("examples/ecm1/ecm1_network.xml"),
        help="Output XML file path",
    ),
) -> None:
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
    endpoint_map = build_ecm1_adjacency(lines_bng)
    route = find_ecm1_route(lines_bng, endpoint_map)

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
    output.parent.mkdir(parents=True, exist_ok=True)
    ElementTree(root).write(str(output), encoding="unicode", xml_declaration=True)

    size_mb = output.stat().st_size / 1_000_000
    print(f"\nWrote {output}  ({size_mb:.1f} MB)")
    print(f"  netElements  : {len(net_elements):,}")
    print(f"  tracks       : {len(tracks):,}")
    print(f"  route tracks : {len(route)}")


if __name__ == "__main__":
    app()
