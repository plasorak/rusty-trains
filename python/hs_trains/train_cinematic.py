"""Interactive animated train visualisation on OpenStreetMap with journey profile.

Requires lon_deg, lat_deg, power_kw columns (produced by hs-trains when the
infrastructure file contains GML track geometry).

Layout:
- **Top (map)**: full route as a static heat-map coloured by speed or power.
- **Bottom (chart)**: speed (km/h, left) and power (kW, right) vs journey
  distance, with animated cursor dots tracking the current frame.  The bottom
  chart is fully zoomable on the x-axis via mouse drag or scroll.

The animation only redraws the two small cursor dots — not the mapbox — so
playback is smooth even at high speed multipliers.

CLI::

    train-cinematic simulation.parquet
    train-cinematic simulation.parquet -o out.html --frame-step 5
    train-cinematic simulation.parquet --metric power
"""

from pathlib import Path

import polars as pl
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import typer

app = typer.Typer(add_completion=False)

# Playback speeds offered to the user: label → frame duration in milliseconds.
_SPEEDS: list[tuple[str, int]] = [
    ("0.5×",  400),
    ("1×",    200),
    ("2×",    100),
    ("5×",     40),
    ("10×",    20),
]


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

    df = df.drop_nulls(subset=["lon_deg", "lat_deg", "speed_kmh", "power_kw"]).sort("time_s")
    if df.is_empty():
        typer.echo("Error: no rows with complete geographic + physics data.", err=True)
        raise typer.Exit(1)

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

    # Stable insertion-ordered unique frame labels.
    seen: set[str] = set()
    unique_frames: list[str] = []
    for f in df["_frame"].to_list():
        if f not in seen:
            unique_frames.append(f)
            seen.add(f)

    # ------------------------------------------------------------------
    # Figure layout: map (top, 65 %) + journey chart (bottom, 35 %)
    #
    # Trace index plan:
    #   0  grey background route line   mapbox — static
    #   1  speed/power heat-map         mapbox — static
    #   2  speed vs distance            xy primary y — static
    #   3  power vs distance            xy secondary y — static
    #   4  speed cursor dot             xy primary y — ANIMATED
    #   5  power cursor dot             xy secondary y — ANIMATED
    #
    # Frames only update traces [4, 5] with redraw=False, so the mapbox is
    # never touched during playback → smooth at any speed multiplier.
    # ------------------------------------------------------------------
    fig = make_subplots(
        rows=2, cols=1,
        row_heights=[0.65, 0.35],
        specs=[[{"type": "mapbox"}], [{"type": "xy", "secondary_y": True}]],
        vertical_spacing=0.04,
    )

    # Trace 0: grey baseline so the route outline is always visible.
    fig.add_trace(go.Scattermapbox(
        lat=lats, lon=lons, mode="lines",
        line=dict(width=2, color="rgba(120,120,120,0.3)"),
        hoverinfo="none", showlegend=False,
    ), row=1, col=1)

    # Trace 1: heat-map of the whole journey.
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

    # Trace 2: speed profile (static line).
    fig.add_trace(go.Scatter(
        x=pos_km, y=speeds, mode="lines",
        name="Speed (km/h)", line=dict(color="royalblue", width=1.5),
    ), row=2, col=1, secondary_y=False)

    # Trace 3: power profile (static line).
    fig.add_trace(go.Scatter(
        x=pos_km, y=powers, mode="lines",
        name="Power (kW)", line=dict(color="firebrick", width=1.5),
    ), row=2, col=1, secondary_y=True)

    # Trace 4: animated speed cursor.
    fig.add_trace(go.Scatter(
        x=[pos_km[0]], y=[speeds[0]], mode="markers",
        marker=dict(size=10, color="royalblue", line=dict(width=2, color="white")),
        showlegend=False, hoverinfo="none",
    ), row=2, col=1, secondary_y=False)

    # Trace 5: animated power cursor.
    fig.add_trace(go.Scatter(
        x=[pos_km[0]], y=[powers[0]], mode="markers",
        marker=dict(size=10, color="firebrick", line=dict(width=2, color="white")),
        showlegend=False, hoverinfo="none",
    ), row=2, col=1, secondary_y=True)

    # ------------------------------------------------------------------
    # Animation frames — only the two cursor dots, no mapbox redraw.
    # ------------------------------------------------------------------
    typer.echo("Building animation frames …")

    # Pre-group by frame to avoid repeated filtering.
    frame_data: dict[str, dict] = {}
    for label in unique_frames:
        fd = df.filter(pl.col("_frame") == label)
        frame_data[label] = {
            "pk":    (fd["position_m"] / 1000.0).to_list(),
            "speed": fd["speed_kmh"].to_list(),
            "power": fd["power_kw"].to_list(),
        }

    fig.frames = [
        go.Frame(
            name=label,
            data=[
                go.Scatter(
                    x=frame_data[label]["pk"],
                    y=frame_data[label]["speed"],
                    mode="markers",
                    marker=dict(size=10, color="royalblue", line=dict(width=2, color="white")),
                ),
                go.Scatter(
                    x=frame_data[label]["pk"],
                    y=frame_data[label]["power"],
                    mode="markers",
                    marker=dict(size=10, color="firebrick", line=dict(width=2, color="white")),
                ),
            ],
            traces=[4, 5],
        )
        for label in unique_frames
    ]

    # ------------------------------------------------------------------
    # Controls: Play/Pause + speed multiplier buttons + time slider.
    #
    # Speed buttons are in the same updatemenus list as Play/Pause.
    # Each speed button re-triggers animate with a different duration.
    # ------------------------------------------------------------------
    default_duration = 200  # ms — matches "1×"

    play_pause = {
        "type": "buttons",
        "showactive": False,
        "direction": "left",
        "y": 0.015, "x": 0.0, "xanchor": "left", "yanchor": "bottom",
        "buttons": [
            {
                "label": "▶ Play",
                "method": "animate",
                "args": [
                    None,
                    {"frame": {"duration": default_duration, "redraw": False},
                     "transition": {"duration": 0},
                     "fromcurrent": True},
                ],
            },
            {
                "label": "⏸ Pause",
                "method": "animate",
                "args": [[None], {"frame": {"duration": 0, "redraw": False}, "mode": "immediate"}],
            },
        ],
    }

    speed_buttons = {
        "type": "buttons",
        "showactive": True,
        "direction": "left",
        "y": 0.015, "x": 0.14, "xanchor": "left", "yanchor": "bottom",
        "buttons": [
            {
                "label": label,
                "method": "animate",
                "args": [
                    None,
                    {"frame": {"duration": dur, "redraw": False},
                     "transition": {"duration": 0},
                     "fromcurrent": True},
                ],
            }
            for label, dur in _SPEEDS
        ],
    }

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
        updatemenus=[play_pause, speed_buttons],
        sliders=[{
            "active": 0,
            "steps": [
                {
                    "args": [
                        [lb],
                        {"frame": {"duration": 0, "redraw": False}, "mode": "immediate"},
                    ],
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
    # fixedrange=False is the Plotly default, but be explicit so future edits
    # don't accidentally lock the axis.
    fig.update_xaxes(
        title_text="Journey distance (km)",
        fixedrange=False,
        row=2, col=1,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(output), include_plotlyjs="cdn")
    size_mb = output.stat().st_size / 1_000_000
    typer.echo(f"Saved {output} ({size_mb:.1f} MB)")

    if open_browser:
        fig.show()
