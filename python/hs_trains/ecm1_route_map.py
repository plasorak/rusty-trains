"""Interactive map of the ECM1 route with junctions and diamond crossings highlighted.

Shows:
  - All ECM1 track segments (grey, thin)
  - The simulated route segments (blue, thick)
  - Y-junction nodes touched by the route (orange dots)
  - Diamond crossing nodes touched by the route (red diamonds)
  - Route start/end buffer stops (green dots)

Usage
-----
    uv run ecm1-route-map
    uv run ecm1-route-map --output my_map.html --no-open
"""

from collections import defaultdict, deque
from itertools import combinations
from pathlib import Path

import geopandas as gpd
import plotly.graph_objects as go
import pyproj
import typer

from hs_trains.gtcl import GPKG, round_bng

_BNG = pyproj.CRS("EPSG:27700")
_WGS84 = pyproj.CRS("EPSG:4326")
_TRANSFORMER = pyproj.Transformer.from_crs(_BNG, _WGS84, always_xy=True)


def _bng_to_wgs84(x: int, y: int) -> tuple[float, float]:
    """Convert a rounded BNG coordinate to (lon, lat)."""
    lon, lat = _TRANSFORMER.transform(x, y)
    return lon, lat


def _build_adjacency(
    lines_bng: gpd.GeoDataFrame,
) -> dict[tuple[int, int], list[str]]:
    """endpoint coordinate → list of ASSETID strings that touch it."""
    endpoint_map: dict[tuple[int, int], list[str]] = defaultdict(list)
    for _, row in lines_bng.iterrows():
        seg_id = str(row["ASSETID"])
        coords = list(row.geometry.coords)
        endpoint_map[round_bng(*coords[0])].append(seg_id)
        endpoint_map[round_bng(*coords[-1])].append(seg_id)
    return endpoint_map


def _find_route(
    lines_bng: gpd.GeoDataFrame,
    endpoint_map: dict[tuple[int, int], list[str]],
) -> list[str]:
    """Same bi-directional BFS as make_ecm1_railml, returning ordered ASSETID list."""
    adj: dict[str, list[str]] = defaultdict(list)
    for segs in endpoint_map.values():
        for s1, s2 in combinations(segs, 2):
            adj[s1].append(s2)
            adj[s2].append(s1)

    deg1 = sorted(
        [(coord, segs[0]) for coord, segs in endpoint_map.items() if len(segs) == 1],
        key=lambda x: x[0][1],
    )
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

    best: tuple[int, str, str] | None = None
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
        route = _reconstruct(goal_seg, parent_s)

    seen: set[str] = set()
    unique: list[str] = []
    for seg in route:
        if seg not in seen:
            seen.add(seg)
            unique.append(seg)
    return unique


def _lines_trace(
    gdf: gpd.GeoDataFrame,
    route_set: set[str],
) -> tuple[go.Scattermapbox, go.Scattermapbox]:
    """Return (background_trace, route_trace) for all ECM1 segments."""
    bg_lats: list[float | None] = []
    bg_lons: list[float | None] = []
    bg_hover: list[str | None] = []
    rt_lats: list[float | None] = []
    rt_lons: list[float | None] = []
    rt_hover: list[str | None] = []

    for _, row in gdf.iterrows():
        coords = list(row.geometry.coords)
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        assetid = str(row["ASSETID"])
        trid = row.get("TRID", "")
        hover = f"ASSETID: {assetid}<br>TRID: {trid}"
        if assetid in route_set:
            rt_lats += lats + [None]
            rt_lons += lons + [None]
            rt_hover += [hover] * len(lats) + [None]
        else:
            bg_lats += lats + [None]
            bg_lons += lons + [None]
            bg_hover += [hover] * len(lats) + [None]

    bg = go.Scattermapbox(
        lat=bg_lats, lon=bg_lons, mode="lines",
        line=dict(width=1, color="#aaaaaa"),
        hoverinfo="text", hovertext=bg_hover,
        name="ECM1 (off-route)",
    )
    rt = go.Scattermapbox(
        lat=rt_lats, lon=rt_lons, mode="lines",
        line=dict(width=3, color="#1a6fca"),
        hoverinfo="text", hovertext=rt_hover,
        name="Route",
    )
    return bg, rt


def _node_traces(
    endpoint_map: dict[tuple[int, int], list[str]],
    route_coords: set[tuple[int, int]],
    route: list[str],
) -> list[go.Scattermapbox]:
    """Build marker traces for junction, crossing, and terminal nodes on the route."""
    junction_lons, junction_lats, junction_hover = [], [], []
    crossing_lons, crossing_lats, crossing_hover = [], [], []
    terminal_lons, terminal_lats, terminal_hover = [], [], []

    route_set = set(route)
    deg1_coords = {c for c, segs in endpoint_map.items() if len(segs) == 1}

    for coord in route_coords:
        segs = endpoint_map.get(coord, [])
        degree = len(segs)
        lon, lat = _bng_to_wgs84(*coord)
        route_segs_here = [s for s in segs if s in route_set]
        seg_list = ", ".join(route_segs_here[:3])
        hover = f"Degree: {degree}<br>Route segs: {seg_list}"

        if coord in deg1_coords:
            terminal_lons.append(lon)
            terminal_lats.append(lat)
            terminal_hover.append(f"Terminal (buffer stop)<br>{hover}")
        elif degree == 4:
            crossing_lons.append(lon)
            crossing_lats.append(lat)
            crossing_hover.append(f"Diamond crossing<br>{hover}")
        elif degree == 3:
            junction_lons.append(lon)
            junction_lats.append(lat)
            junction_hover.append(f"Y-junction<br>{hover}")

    traces = []
    if junction_lons:
        traces.append(go.Scattermapbox(
            lat=junction_lats, lon=junction_lons, mode="markers",
            marker=dict(size=6, color="#f59e0b", opacity=0.7),
            hoverinfo="text", hovertext=junction_hover,
            name=f"Y-junctions on route ({len(junction_lons)})",
            visible="legendonly",
        ))
    if crossing_lons:
        # Two overlapping markers: large white halo + smaller red circle.
        # Scattermapbox only supports Maki icon names; "star" is not valid.
        traces.append(go.Scattermapbox(
            lat=crossing_lats, lon=crossing_lons, mode="markers+text",
            marker=dict(size=22, color="white", opacity=1.0),
            hoverinfo="skip",
            showlegend=False,
        ))
        traces.append(go.Scattermapbox(
            lat=crossing_lats, lon=crossing_lons, mode="markers+text",
            marker=dict(size=16, color="#e03030", opacity=1.0),
            text=["◆"] * len(crossing_lons),
            textposition="top right",
            textfont=dict(size=14, color="#e03030"),
            hoverinfo="text", hovertext=crossing_hover,
            name=f"Diamond crossings on route ({len(crossing_lons)})",
        ))
    if terminal_lons:
        traces.append(go.Scattermapbox(
            lat=terminal_lats, lon=terminal_lons, mode="markers",
            marker=dict(size=10, color="#16a34a", symbol="circle"),
            hoverinfo="text", hovertext=terminal_hover,
            name="Route terminals",
        ))
    return traces


app = typer.Typer()


@app.command()
def main(
    output: Path = typer.Option(Path("ecm1_route_map.html"), help="Output HTML path"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open in browser after saving"),
) -> None:
    if not GPKG.exists():
        raise SystemExit(f"GeoPackage not found: {GPKG}")

    typer.echo("Loading ECM1 segments …")
    lines_bng: gpd.GeoDataFrame = gpd.read_file(GPKG, layer="NWR_GTCL")
    lines_bng = lines_bng[
        (lines_bng["SUPERCEDED"] != "YES") & (lines_bng["ELR"] == "ECM1")
    ].reset_index(drop=True)
    lines_wgs84 = lines_bng.to_crs("EPSG:4326")

    typer.echo("Finding route …")
    endpoint_map = _build_adjacency(lines_bng)
    route = _find_route(lines_bng, endpoint_map)
    route_set = set(route)

    # Collect all endpoint coordinates that the route passes through.
    assetid_coords: dict[str, list[tuple[int, int]]] = {}
    for _, row in lines_bng.iterrows():
        coords = list(row.geometry.coords)
        assetid_coords[str(row["ASSETID"])] = [
            round_bng(*coords[0]), round_bng(*coords[-1])
        ]
    route_coords: set[tuple[int, int]] = set()
    for assetid in route:
        for c in assetid_coords.get(assetid, []):
            route_coords.add(c)

    junction_count = sum(1 for c in route_coords if len(endpoint_map.get(c, [])) == 3)
    crossing_count = sum(1 for c in route_coords if len(endpoint_map.get(c, [])) == 4)
    typer.echo(f"  Route: {len(route)} segments, {junction_count} Y-junctions, {crossing_count} diamond crossings")

    typer.echo("Building figure …")
    bg_trace, route_trace = _lines_trace(lines_wgs84, route_set)
    node_traces = _node_traces(endpoint_map, route_coords, route)

    centre_lat = float(lines_wgs84.geometry.apply(lambda g: g.centroid.y).mean())
    centre_lon = float(lines_wgs84.geometry.apply(lambda g: g.centroid.x).mean())

    fig = go.Figure(data=[bg_trace, route_trace, *node_traces])
    fig.update_layout(
        title="ECM1 — simulated route with junctions and diamond crossings",
        mapbox=dict(
            style="open-street-map",
            center=dict(lat=centre_lat, lon=centre_lon),
            zoom=7,
        ),
        legend=dict(bgcolor="rgba(255,255,255,0.9)", bordercolor="gray", borderwidth=1),
        margin=dict(l=0, r=0, t=40, b=0),
        height=900,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(output), include_plotlyjs="cdn")
    typer.echo(f"Saved {output} ({output.stat().st_size / 1e6:.1f} MB)")

    if open_browser:
        fig.show()


if __name__ == "__main__":
    app()
