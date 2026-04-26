"""Interactive animated train visualisation on OpenStreetMap with journey profile.

Requires lon_deg, lat_deg, power_kw columns (produced by hs-trains when the
infrastructure file contains GML track geometry).

The output is a self-contained HTML file with two panels:

- **Top (map)**: full route drawn as a static heat-map coloured by speed (or
  power), with an animated white dot showing the current train position.
- **Bottom (chart)**: speed (km/h, left axis) and power (kW, right axis) plotted
  against journey distance (km), with animated cursor dots tracking the current
  position.

CLI::

    train-cinematic simulation.parquet
    train-cinematic simulation.parquet -o out.html --frame-step 5
    train-cinematic simulation.parquet --metric power
"""

from pathlib import Path
from typing import Any

import polars as pl
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import typer

app = typer.Typer(add_completion=False)


@app.command()
def main(
    parquet: Path = typer.Argument(..., help="Simulation output Parquet file"),
    output: Path = typer.Option(Path("train_cinematic.html"), "-o", "--output", help="Output HTML path"),
    frame_step: int = typer.Option(1, "--frame-step", help="Keep every Nth time step as an animation frame"),
    metric: str = typer.Option("speed", "--metric", help="Map colour metric: 'speed' or 'power'"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open in browser after saving"),
) -> None:
    if metric not in ("speed", "power"):
        typer.echo("--metric must be 'speed' or 'power'", err=True)
        raise typer.Exit(1)

    typer.echo(f"Loading {parquet} …")
    df = pl.read_parquet(parquet)

    if "event_kind" in df.schema:
        df = df.filter(pl.col("event_kind") == "physics_tick")

    required = ["lon_deg", "lat_deg", "speed_kmh", "power_kw", "position_m"]
    missing = [c for c in required if c not in df.schema]
    if missing:
        typer.echo(
            f"Error: missing columns {missing}.\n"
            "Re-run the simulation with an infrastructure file that contains GML track geometry.",
            err=True,
        )
        raise typer.Exit(1)

    df = (
        df
        .drop_nulls(subset=["lon_deg", "lat_deg", "speed_kmh", "power_kw"])
        .sort("time_s")
    )
    if df.is_empty():
        typer.echo("Error: no rows with complete geographic + physics data.", err=True)
        raise typer.Exit(1)

    # Down-sample time steps to reduce frame count / file size.
    all_times = sorted(df["time_s"].unique().to_list())
    df = df.filter(pl.col("time_s").is_in(all_times[::frame_step]))
    df = df.with_columns(pl.col("time_s").cast(pl.Utf8).alias("_frame"))

    n_frames = df["_frame"].n_unique()
    n_trains = df["train_id"].n_unique()
    typer.echo(f"  {n_trains} train(s), {n_frames} animation frames")

    lats      = df["lat_deg"].to_list()
    lons      = df["lon_deg"].to_list()
    speeds    = df["speed_kmh"].to_list()
    powers    = df["power_kw"].to_list()
    pos_km    = (df["position_m"] / 1000.0).to_list()
    times_s   = df["time_s"].cast(pl.Float64).to_list()
    train_ids = df["train_id"].to_list()

    max_speed = float(df["speed_kmh"].max() or 120.0)
    max_power = float(df["power_kw"].max() or 1.0)

    map_values     = speeds    if metric == "speed" else powers
    map_max        = max_speed if metric == "speed" else max_power
    map_label      = "Speed (km/h)" if metric == "speed" else "Power (kW)"
    map_colorscale = "RdYlGn"       if metric == "speed" else "YlOrRd"

    # Ordered unique frame labels (insertion order preserved).
    seen: set[str] = set()
    unique_frames: list[str] = []
    for f in df["_frame"].to_list():
        if f not in seen:
            unique_frames.append(f)
            seen.add(f)

    # ------------------------------------------------------------------
    # Build figure: map (row 1, 65 %) + journey profile (row 2, 35 %)
    #
    # Trace index plan (in add_trace order):
    #   0  grey background route       mapbox, static
    #   1  heat-map route              mapbox, static
    #   2  current position dot        mapbox, animated
    #   3  speed vs distance           xy row 2 primary y, static
    #   4  power vs distance           xy row 2 secondary y, static
    #   5  speed cursor dot            xy row 2 primary y, animated
    #   6  power cursor dot            xy row 2 secondary y, animated
    # ------------------------------------------------------------------
    fig = make_subplots(
        rows=2, cols=1,
        row_heights=[0.65, 0.35],
        specs=[[{"type": "mapbox"}], [{"type": "xy", "secondary_y": True}]],
        vertical_spacing=0.04,
    )

    # --- Row 1: map ---------------------------------------------------

    # Trace 0: grey background line so the full route is always visible.
    fig.add_trace(go.Scattermapbox(
        lat=lats, lon=lons, mode="lines",
        line=dict(width=2, color="rgba(120,120,120,0.3)"),
        hoverinfo="none", showlegend=False, name="_bg",
    ), row=1, col=1)

    # Trace 1: colored heat-map of the full journey (small markers ≈ line).
    hover_map = [
        f"{tid}<br>t = {t:.0f} s<br>{s:.1f} km/h  {p:.0f} kW"
        for tid, t, s, p in zip(train_ids, times_s, speeds, powers)
    ]
    fig.add_trace(go.Scattermapbox(
        lat=lats, lon=lons, mode="markers",
        marker=dict(
            size=5, color=map_values,
            colorscale=map_colorscale, cmin=0, cmax=map_max,
            colorbar=dict(
                title=map_label, x=1.02, len=0.55, y=0.73,
                thickness=14, tickfont=dict(size=10),
            ),
            showscale=True,
        ),
        hoverinfo="text", hovertext=hover_map, name=map_label,
    ), row=1, col=1)

    # Trace 2: animated current-position dot.
    first = df.filter(pl.col("_frame") == unique_frames[0])
    fig.add_trace(go.Scattermapbox(
        lat=first["lat_deg"].to_list(),
        lon=first["lon_deg"].to_list(),
        mode="markers",
        marker=dict(size=14, color="white"),
        hoverinfo="none", showlegend=False, name="Position",
    ), row=1, col=1)

    # --- Row 2: journey profile ---------------------------------------

    # Trace 3: speed vs distance (left y-axis).
    fig.add_trace(go.Scatter(
        x=pos_km, y=speeds, mode="lines",
        name="Speed (km/h)",
        line=dict(color="royalblue", width=1.5),
    ), row=2, col=1, secondary_y=False)

    # Trace 4: power vs distance (right y-axis).
    fig.add_trace(go.Scatter(
        x=pos_km, y=powers, mode="lines",
        name="Power (kW)",
        line=dict(color="firebrick", width=1.5),
    ), row=2, col=1, secondary_y=True)

    # Trace 5: animated speed cursor dot.
    fig.add_trace(go.Scatter(
        x=[pos_km[0]], y=[speeds[0]], mode="markers",
        marker=dict(size=10, color="royalblue", line=dict(width=2, color="white")),
        showlegend=False, hoverinfo="none",
    ), row=2, col=1, secondary_y=False)

    # Trace 6: animated power cursor dot.
    fig.add_trace(go.Scatter(
        x=[pos_km[0]], y=[powers[0]], mode="markers",
        marker=dict(size=10, color="firebrick", line=dict(width=2, color="white")),
        showlegend=False, hoverinfo="none",
    ), row=2, col=1, secondary_y=True)

    # ------------------------------------------------------------------
    # Animation frames — only the three animated traces need updating.
    # ------------------------------------------------------------------
    typer.echo("Building animation frames …")

    # Pre-group rows by frame label to avoid repeated filter calls.
    frame_rows: dict[str, dict[str, Any]] = {}
    for label in unique_frames:
        fd = df.filter(pl.col("_frame") == label)
        frame_rows[label] = {
            "lat":   fd["lat_deg"].to_list(),
            "lon":   fd["lon_deg"].to_list(),
            "pk":    (fd["position_m"] / 1000.0).to_list(),
            "speed": fd["speed_kmh"].to_list(),
            "power": fd["power_kw"].to_list(),
        }

    frames = []
    for label in unique_frames:
        d = frame_rows[label]
        frames.append(go.Frame(
            name=label,
            data=[
                go.Scattermapbox(
                    lat=d["lat"], lon=d["lon"], mode="markers",
                    marker=dict(size=14, color="white"),
                ),
                go.Scatter(
                    x=d["pk"], y=d["speed"], mode="markers",
                    marker=dict(size=10, color="royalblue", line=dict(width=2, color="white")),
                ),
                go.Scatter(
                    x=d["pk"], y=d["power"], mode="markers",
                    marker=dict(size=10, color="firebrick", line=dict(width=2, color="white")),
                ),
            ],
            traces=[2, 5, 6],
        ))
    fig.frames = frames

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    centre = dict(lat=sum(lats) / len(lats), lon=sum(lons) / len(lons))

    fig.update_layout(
        title=dict(text="Train simulation — speed & power along route", x=0.5),
        mapbox=dict(style="open-street-map", center=centre, zoom=6),
        height=1000,
        margin=dict(l=50, r=70, t=50, b=90),
        legend=dict(
            x=0.01, y=0.365,
            bgcolor="rgba(255,255,255,0.85)", bordercolor="grey", borderwidth=1,
        ),
        updatemenus=[{
            "type": "buttons", "showactive": False,
            "y": 0.015, "x": 0.0, "xanchor": "left", "yanchor": "bottom",
            "buttons": [
                {
                    "label": "▶ Play",
                    "method": "animate",
                    "args": [None, {"frame": {"duration": 150, "redraw": True}, "fromcurrent": True}],
                },
                {
                    "label": "⏸ Pause",
                    "method": "animate",
                    "args": [[None], {"frame": {"duration": 0, "redraw": False}, "mode": "immediate"}],
                },
            ],
        }],
        sliders=[{
            "active": 0,
            "steps": [
                {
                    "args": [[lb], {"frame": {"duration": 0, "redraw": True}, "mode": "immediate"}],
                    "label": f"{float(lb) / 3600:.2f} h",
                    "method": "animate",
                }
                for lb in unique_frames
            ],
            "currentvalue": {"prefix": "Time: ", "suffix": " s", "visible": True},
            "pad": {"t": 10, "b": 10}, "y": 0, "x": 0.07, "len": 0.88,
        }],
    )

    fig.update_yaxes(
        title_text="Speed (km/h)",
        title_font=dict(color="royalblue"),
        tickfont=dict(color="royalblue"),
        row=2, col=1, secondary_y=False,
    )
    fig.update_yaxes(
        title_text="Power (kW)",
        title_font=dict(color="firebrick"),
        tickfont=dict(color="firebrick"),
        row=2, col=1, secondary_y=True,
    )
    fig.update_xaxes(title_text="Journey distance (km)", row=2, col=1)

    output.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(output), include_plotlyjs="cdn")
    size_mb = output.stat().st_size / 1_000_000
    typer.echo(f"Saved {output} ({size_mb:.1f} MB)")

    if open_browser:
        fig.show()
