import os
import time
import subprocess
import re
import sys
import sqlite3
import logging
import socket
import asyncio
import datetime
import threading
from pathlib import Path

import dash
import pandas as pd
from dash import dcc, html, dash_table
from dash.dependencies import Input, Output, State
from flask import jsonify
from flask_caching import Cache
from tapo import ApiClient

# ---------- Logging ----------

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
_level = getattr(logging, LOG_LEVEL, logging.INFO)
logging.basicConfig(
    level=_level,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)
logger.info(f"Log level set to {logging.getLevelName(_level)}")

# ---------- Paths & config ----------

# Directory of THIS file (works under gunicorn)
BASE_DIR = Path(__file__).resolve().parent

# Single source of truth for DB path (override with env if you like)
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "data" / "internet_status.db"))

# Redis URL (optional). If unset or empty, fall back to in-process SimpleCache.
REDIS_URL = os.environ.get("REDIS_URL", "").strip()

# Timezone for display (keep storage/filtering in UTC)
DISPLAY_TZ = os.environ.get("DISPLAY_TZ", "UTC")

# Log effective config
try:
    db_exists = Path(DB_PATH).exists()
    db_size = Path(DB_PATH).stat().st_size if db_exists else 0
    logger.info(
        f"Config: DB_PATH={DB_PATH} (exists={db_exists}, size={db_size} bytes), "
        f"REDIS_URL={REDIS_URL}, DISPLAY_TZ={DISPLAY_TZ}"
    )
except Exception:
    logger.info(f"Config: DB_PATH={DB_PATH}, REDIS_URL={REDIS_URL}, DISPLAY_TZ={DISPLAY_TZ}")

# ---------- Dash app ----------

app = dash.Dash(__name__)
server = app.server  # expose Flask server for caching / healthcheck

# Add custom CSS for dropdown styling
app.index_string = '''
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>{%title%}</title>
        {%favicon%}
        {%css%}
        <style>
            .Select-menu-outer {
                background-color: #1e1e1e !important;
                border: 1px solid #333 !important;
            }
            .Select-option {
                background-color: #1e1e1e !important;
                color: #ffffff !important;
                padding: 8px 12px !important;
            }
            .Select-option:hover {
                background-color: #333 !important;
                color: #00ccff !important;
            }
            .Select-option.is-focused {
                background-color: #333 !important;
                color: #00ccff !important;
            }
            .Select-option.is-selected {
                background-color: #00ccff !important;
                color: #1e1e1e !important;
            }
            .dropdown .Select-control {
                background-color: #121212 !important;
                border-color: #333 !important;
            }
            .dropdown .Select-value-label {
                color: #ffffff !important;
            }
            @keyframes pulse {
                0% { opacity: 0.6; }
                50% { opacity: 1; }
                100% { opacity: 0.6; }
            }
            .speed-test-pulse {
                animation: pulse 1.5s ease-in-out infinite;
            }
            @keyframes barberPole {
                0% { background-position: 0 0; }
                100% { background-position: 40px 0; }
            }
            .speed-test-loading {
                background: linear-gradient(
                    45deg,
                    transparent 25%,
                    rgba(255,255,255,0.2) 25%,
                    rgba(255,255,255,0.2) 50%,
                    transparent 50%,
                    transparent 75%,
                    rgba(255,255,255,0.2) 75%
                );
                background-size: 40px 40px;
                animation: barberPole 1s linear infinite;
            }
        </style>
    </head>
    <body>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>
'''

if REDIS_URL:
    cache = Cache(
        app.server,
        config={
            "CACHE_TYPE": "redis",
            "CACHE_REDIS_URL": REDIS_URL,
            "CACHE_DEFAULT_TIMEOUT": 60,
        },
    )
    logger.info(f"Cache: Using Redis backend at {REDIS_URL}")
else:
    cache = Cache(
        app.server,
        config={
            "CACHE_TYPE": "SimpleCache",
            "CACHE_DEFAULT_TIMEOUT": 60,
        },
    )
    logger.info("Cache: Using in-process SimpleCache backend")

# ---------- Helpers ----------

# Shared visual style for badge-like UI elements (status + button)
BADGE_BASE_STYLE = {
    "textAlign": "center",
    "fontSize": "18px",
    "fontWeight": "bold",
    "fontFamily": "Arial, sans-serif",
    "padding": "0 15px",
    "borderRadius": "5px",
    "minWidth": "220px",
    "height": "40px",
    "display": "inline-flex",
    "alignItems": "center",
    "justifyContent": "center",
    "boxSizing": "border-box",
}

def _connect_ro(db_path: str) -> sqlite3.Connection:
    """Open SQLite for reading. Use a normal connection to allow SQLite to
    create/access WAL/SHM files, avoiding 'readonly database' errors during WAL.
    """
    return sqlite3.connect(db_path)

# Safe TZ convert helper: keep UTC if conversion fails

def _to_display_tz(ts: pd.Series) -> pd.Series:
    """Convert a tz-aware pandas datetime Series (UTC) to DISPLAY_TZ for UI.
    Falls back to UTC if zone data is missing/invalid.
    """
    try:
        return ts.dt.tz_convert(DISPLAY_TZ)
    except Exception as e:
        # If zoneinfo data isn't present in the container, keep UTC and warn once
        logger.warning(f"Could not apply DISPLAY_TZ='{DISPLAY_TZ}', keeping UTC: {e}")
        return ts

# --- Live internet status check for the badge ---
def check_live_internet_status_for_badge():
    # Use just the first target for quick badge responsiveness
    raw_targets = os.environ.get("INTERNET_CHECK_TARGETS", "8.8.8.8,1.1.1.1,9.9.9.9")
    targets = [ip.strip() for ip in raw_targets.split(",") if ip.strip()]
    target = targets[0] if targets else "8.8.8.8"  # Use first target only

    ping_timeout = 1  # 1s timeout for responsiveness

    try:
        # Single ping for speed
        command = ["ping", "-c", "1", "-W", str(ping_timeout), target]
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=ping_timeout + 1
        )
        ping_output = result.stdout.strip()
        logger.debug(f"Internet status badge, ping output: {ping_output}")

        if result.returncode == 0:
            # Success - internet is up
            return "Internet: Up", "#4CAF50"         # green
        else:
            logger.debug(f"Ping to {target} failed: {result.stderr.strip()}")
            return "Internet: Down", "#ff6666"       # red
    except Exception as e:
        logger.error(f"Error during live ping check for {target}: {e}")
        return "Internet: Down", "#ff6666"       # red

# ---------- Data access ----------

def parse_log(db_path: str) -> pd.DataFrame:
    """Fetch all records from internet_status."""
    try:
        with _connect_ro(db_path) as conn:
            query = """
            SELECT timestamp,
                   status AS status_message,
                   success_percentage AS success,
                   avg_latency_ms,
                   max_latency_ms,
                   min_latency_ms,
                   packet_loss
            FROM internet_status
            """
            df = pd.read_sql_query(query, conn)

        # Timestamp to UTC
        if not df.empty:
            df["timestamp"] = pd.to_datetime(df["timestamp"], format="mixed", utc=True)

            # Ensure numeric
            numeric_cols = ["success", "avg_latency_ms", "max_latency_ms", "min_latency_ms", "packet_loss"]
            for col in numeric_cols:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            # Cap extremes
            df["avg_latency_ms"] = df["avg_latency_ms"].clip(upper=500)
            df["max_latency_ms"] = df["max_latency_ms"].clip(upper=500)
            df["min_latency_ms"] = df["min_latency_ms"].clip(upper=500)
            df["packet_loss"]   = df["packet_loss"].clip(upper=100)

        logger.debug("Data parsed successfully from the database.")
        return df
    except Exception as e:
        logger.error(f"Error parsing log: {e}")
        return pd.DataFrame()

def filter_data_by_date(log_data: pd.DataFrame, date_range: str) -> pd.DataFrame:
    """Filter by preset ranges (all math done in UTC)."""
    if log_data.empty:
        return log_data

    now = pd.to_datetime(datetime.datetime.utcnow()).tz_localize("UTC")

    if date_range == "last_12_hours":
        start = now - pd.DateOffset(hours=12)
    elif date_range == "last_24_hours":
        start = now - pd.DateOffset(hours=24)
    elif date_range == "last_48_hours":
        start = now - pd.DateOffset(hours=48)
    elif date_range == "last_7_days":
        start = now - pd.DateOffset(days=7)
    else:
        return log_data  # all_time

    return log_data[log_data["timestamp"] >= start]

@cache.memoize(timeout=30)
def get_filtered_data(db_path: str, date_range: str):
    """Fetch + filter in UTC, then convert timestamps to DISPLAY_TZ for UI, memoized in Redis."""
    try:
        df = parse_log(db_path)                # df['timestamp'] is UTC
        if df.empty:
            logger.warning("Parsed DataFrame is empty.")
            return []
        fdf = filter_data_by_date(df, date_range).copy()  # filter in UTC
        if fdf.empty:
            logger.warning("Filtered DataFrame is empty after applying date range.")
            return []

        # Convert timestamps to display TZ for UI only
        fdf["timestamp"] = _to_display_tz(fdf["timestamp"])  # safe convert

        cols = [
            "timestamp",
            "status_message",
            "success",
            "avg_latency_ms",
            "max_latency_ms",
            "min_latency_ms",
            "packet_loss",
        ]
        logger.debug(f"Returning filtered data with {len(fdf)} records.")
        return fdf[cols].to_dict("records")
    except Exception as e:
        logger.error(f"Redis Cache Error: {e}")
        # Fallback (non-memoized)
        df = parse_log(db_path)
        fdf = filter_data_by_date(df, date_range)
        if not fdf.empty:
            fdf["timestamp"] = _to_display_tz(fdf["timestamp"])  # safe convert
        return fdf.to_dict("records") if not fdf.empty else []

def calculate_y_range(series: pd.Series, absolute_max: float, buffer_ratio: float = 0.1):
    if series.empty:
        return [0, absolute_max]
    dynamic_max = float(series.max()) * (1 + buffer_ratio)
    return [0, min(dynamic_max, absolute_max)]

def is_internet_up() -> bool:
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=2)
        return True
    except OSError:
        return False

def run_speed_test():
    """Run speed test using Python speedtest library"""
    try:
        import speedtest

        logger.info("Starting speed test...")
        st = speedtest.Speedtest()

        # Get best server based on ping
        st.get_best_server()
        server_info = st.results.server

        # Run download test
        logger.info("Running download test...")
        download_speed = st.download()

        # Run upload test
        logger.info("Running upload test...")
        upload_speed = st.upload()

        # Get ping
        ping_ms = st.results.ping

        # Convert bits/s to Mbps
        download_mbps = round(download_speed / 1_000_000, 2)
        upload_mbps = round(upload_speed / 1_000_000, 2)

        server_name = server_info.get("sponsor", "Unknown")
        server_location = f"{server_info.get('name', '')}, {server_info.get('country', '')}"

        logger.info(f"Speed test completed: {download_mbps}↓ {upload_mbps}↑ {ping_ms}ms")

        return {
            "success": True,
            "download": download_mbps,
            "upload": upload_mbps,
            "ping": round(ping_ms, 1),
            "server": server_name,
            "location": server_location.strip(", ")
        }

    except ImportError:
        logger.error("speedtest-py library not installed. Install with: pip install speedtest-cli")
        return {"success": False, "error": "speedtest-py library not installed"}
    except Exception as e:
        logger.error(f"Speed test error: {e}")
        return {"success": False, "error": f"Speed test failed: {str(e)}"}

def check_tapo_connection():
    email = os.environ.get("TAPO_EMAIL")
    password = os.environ.get("TAPO_PASSWORD")
    device_ip = os.environ.get("TAPO_DEVICE_IP")

    # Avoid logging credentials; only log at debug level if needed
    logger.debug("Tapo connectivity check invoked")

    if not all([email, password, device_ip]):
        logger.warning("Tapo credentials (email, password, or IP) are not set as environment variables.")
        return False, "Tapo credentials missing."

    try:
        # Simple socket test to device IP instead of full Tapo API
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((device_ip, 80))
        sock.close()

        if result == 0:
            logger.debug("Tapo device reachable")
            return True, "Tapo device reachable."
        else:
            logger.debug("Tapo device unreachable")
            return False, "Tapo device unreachable."
    except Exception as e:
        # Log a concise warning without credentials
        logger.warning(f"Tapo connectivity failed: {e}")
        return False, f"Failed to connect to Tapo device: {e}"

# ---------- Layout ----------

app.layout = html.Div(
    [
        html.Div(
            [html.H1("Network Health Monitoring", style={"color": "#00ccff", "margin": "0", "textAlign": "center"})],
            style={
                "padding": "10px 0",
                "backgroundColor": "#1e1e1e",
                "borderRadius": "8px",
                "marginBottom": "20px",
                "fontFamily": "Arial, sans-serif",
            },
        ),
        html.Div(
            [
                html.Div(
                    id="internet-status",
                    className="badge status-badge",
                    style={"color": "#FFFFFF"},
                ),
                html.Div(
                    [
                        html.Button(
                            "Restart NBN",
                            id="power-cycle-button",
                            n_clicks=0,
                            className="badge action-button",
                            style={"cursor": "pointer"},
                        ),
                    ],
                    style={"display": "flex", "alignItems": "center"},
                ),
            ],
            className="toolbar",
            style={
                "display": "flex",
                "alignItems": "center",
                "justifyContent": "space-between",
                "backgroundColor": "#1e1e1e",
                "padding": "10px 20px",
                "borderRadius": "8px",
                "marginBottom": "20px",
            },
        ),
        # Speed Test Section
        html.Div(
            id="speed-test-section",
            children=[
                html.Div(
                    [
                        html.Button(
                            "Speed Test",
                            id="speed-test-button",
                            n_clicks=0,
                            className="badge action-button",
                            style={
                                "cursor": "pointer",
                                "backgroundColor": "#888888",
                                "color": "#1e1e1e",
                            },
                        ),
                    ],
                    style={"textAlign": "right", "margin": "0"}
                ),
                html.Div(
                    id="speed-test-visualization",
                    style={"display": "none"},
                    children=[
                        html.H4("Running Speed Test...", style={"color": "#00ccff", "textAlign": "center"}),
                        html.Div(
                            [
                                html.Div("📊 Testing Internet Speed", id="speed-test-phase", style={"color": "#ffffff", "margin": "10px 0", "textAlign": "center"}),
                                html.Div(
                                    style={
                                        "backgroundColor": "#333",
                                        "borderRadius": "10px",
                                        "height": "20px",
                                        "overflow": "hidden",
                                        "position": "relative"
                                    },
                                    children=[
                                        html.Div(
                                            id="speed-test-progress",
                                            style={
                                                "backgroundColor": "#00ccff",
                                                "height": "100%",
                                                "width": "100%",
                                                "position": "absolute",
                                                "borderRadius": "10px",
                                                "display": "none"
                                            }
                                        )
                                    ]
                                ),
                            ],
                            style={"textAlign": "center", "padding": "20px 0"}
                        ),
                    ]
                ),
                html.Div(
                    id="speed-test-results-display",
                    style={"display": "none"},
                ),
            ],
            style={
                "backgroundColor": "#1e1e1e",
                "padding": "10px 20px",
                "borderRadius": "8px",
                "marginBottom": "20px",
            }
        ),
        html.Div(
            [
                html.Div(
                    [
                        html.H4("Date Range", style={"color": "#ffffff"}),
                        dcc.Dropdown(
                            id="date-range-dropdown",
                            options=[
                                {"label": "Last 12 Hours", "value": "last_12_hours"},
                                {"label": "Last 24 Hours", "value": "last_24_hours"},
                                {"label": "Last 48 Hours", "value": "last_48_hours"},
                                {"label": "Last 7 Days", "value": "last_7_days"},
                                {"label": "All Time", "value": "all_time"},
                            ],
                            value="last_12_hours",
                            clearable=False,
                            style={"backgroundColor": "#121212", "color": "#ffffff", "width": "100%"},
                            className="dropdown",
                        ),
                    ],
                    style={"width": "48%", "display": "inline-block"},
                ),
                html.Div(
                    [
                        html.H4("Refresh Rate", style={"color": "#ffffff"}),
                        dcc.Dropdown(
                            id="refresh-rate-dropdown",
                            options=[
                                {"label": "30 seconds", "value": 30},
                                {"label": "1 minute", "value": 60},
                                {"label": "2 minutes", "value": 120},
                                {"label": "5 minutes", "value": 300},
                                {"label": "Manual only (use browser refresh)", "value": 0},
                            ],
                            value=60,
                            clearable=False,
                            style={"backgroundColor": "#121212", "color": "#ffffff", "width": "100%"},
                            className="dropdown",
                        ),
                    ],
                    style={"width": "48%", "display": "inline-block", "marginLeft": "4%"},
                ),
            ],
            className="section",
            style={"display": "flex", "justifyContent": "space-between"},
        ),
        dcc.Store(id="filtered-data"),
        dcc.Store(id="button-state-store"),
        dcc.Store(id="power-cycle-start"),
        dcc.Store(id="tapo-connection-status"),
        dcc.Store(id="is-compact"),
        dcc.Store(id="refresh-interval-store", data=60),
        dcc.Store(id="speed-test-results"),
        dcc.Store(id="speed-test-running", data=False),
        html.Div(
            [
                html.Div(
                    [
                        html.H4(id="full-up-count", className="stat", style={"color": "#00ccff"}),
                        html.H4(id="partial-up-count", className="stat", style={"color": "#ffcc00"}),
                        html.H4(id="down-count", className="stat", style={"color": "#ff6666"}),
                    ],
                    className="stats-row",
                )
            ],
            style={"backgroundColor": "#1e1e1e", "padding": "10px", "borderRadius": "8px", "marginTop": "10px"},
        ),
        dcc.Loading(
            dcc.Graph(
                id="success-graph",
                className="viz-graph",
                config={"responsive": True},
                style={"height": "440px", "width": "100%"},
            ),
            type="default",
        ),
        html.Div(
            [
                dcc.Checklist(
                    id="latency-metrics-checkbox",
                    options=[
                        {"label": "Average Latency (ms)", "value": "avg_latency_ms"},
                        {"label": "Maximum Latency (ms)", "value": "max_latency_ms"},
                        {"label": "Minimum Latency (ms)", "value": "min_latency_ms"},
                    ],
                    value=["avg_latency_ms", "max_latency_ms", "min_latency_ms"],
                    labelStyle={"display": "inline-block", "marginRight": "10px", "color": "#ffffff"},
                    inputStyle={"marginRight": "5px"},
                )
            ],
            className="section",
        ),
        dcc.Loading(
            dcc.Graph(
                id="latency-graph",
                className="viz-graph",
                config={"responsive": True},
                style={"height": "440px", "width": "100%"},
            ),
            type="default",
        ),
        html.Div([], className="section"),
        dcc.Loading(
            dcc.Graph(
                id="packetloss-graph",
                className="viz-graph",
                config={"responsive": True},
                style={"height": "440px", "width": "100%"},
            ),
            type="default",
        ),
        html.Div(
            [
                html.H4("Detailed Log Entries", style={"color": "#ffffff"}),
                dcc.Loading(
                    dash_table.DataTable(
                        id="log-table",
                        columns=[
                            {"name": "Timestamp", "id": "timestamp"},
                            {"name": "Status Message", "id": "status_message"},
                            {"name": "Success (%)", "id": "success"},
                            {"name": "Avg Latency (ms)", "id": "avg_latency_ms"},
                            {"name": "Max Latency (ms)", "id": "max_latency_ms"},
                            {"name": "Min Latency (ms)", "id": "min_latency_ms"},
                            {"name": "Packet Loss (%)", "id": "packet_loss"},
                        ],
                        style_table={"overflowX": "auto", "backgroundColor": "#333", "color": "#fff"},
                        style_cell={"textAlign": "left", "backgroundColor": "#333", "color": "#fff"},
                        page_size=10,
                    ),
                    type="default",
                ),
            ],
            style={"marginTop": "20px", "backgroundColor": "#1e1e1e", "padding": "10px", "borderRadius": "8px"},
        ),
        # Important: keep these cadences distinct.
        # - interval-component (dynamic): drives data fetches for graphs/table.
        # - internet-interval (2s): drives live internet badge + compact-mode.
        # Changing one to match the other will either make the badge laggy
        # or spam the backend with frequent data queries.
        dcc.Interval(id="interval-component", interval=60 * 1000, n_intervals=0),  # dynamic refresh (graphs/table)
        dcc.Interval(id="internet-interval", interval=2 * 1000, n_intervals=0),   # badge + compact-mode every 2s
    ],
    style={"backgroundColor": "#121212", "padding": "20px"},
)

# ---------- Callbacks ----------

@app.callback(
    [Output("interval-component", "interval"), Output("interval-component", "disabled")],
    Input("refresh-rate-dropdown", "value")
)
def update_refresh_interval(refresh_rate):
    if refresh_rate == 0:  # Manual only
        return 60 * 1000, True  # Keep interval but disable it
    else:
        return refresh_rate * 1000, False  # Enable with selected interval

@app.callback(Output("tapo-connection-status", "data"), Input("internet-interval", "n_intervals"))
def update_tapo_status(n):
    connected, message = check_tapo_connection()
    return {"connected": connected, "message": message}

# Global variable to store speed test thread
speed_test_thread = None
speed_test_result = None

@app.callback(
    [
        Output("speed-test-visualization", "style"),
        Output("speed-test-results-display", "style"),
        Output("speed-test-results-display", "children"),
        Output("speed-test-running", "data"),
        Output("speed-test-button", "disabled"),
        Output("speed-test-button", "children"),
    ],
    [Input("speed-test-button", "n_clicks"), Input("internet-interval", "n_intervals")],
    [State("speed-test-running", "data")],
    prevent_initial_call=True
)
def handle_speed_test(n_clicks, n_intervals, is_running):
    global speed_test_thread, speed_test_result

    ctx = dash.callback_context
    if not ctx.triggered:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update, dash.no_update, dash.no_update

    trigger = ctx.triggered[0]["prop_id"].split(".")[0]

    if trigger == "speed-test-button" and n_clicks and not is_running:
        # Start speed test in background thread
        def run_test():
            global speed_test_result
            speed_test_result = run_speed_test()

        speed_test_thread = threading.Thread(target=run_test)
        speed_test_thread.daemon = True
        speed_test_thread.start()

        return (
            {"display": "block"},  # Show visualization
            {"display": "none"},   # Hide results
            [],                    # Clear results
            True,                  # Set running state
            True,                  # Disable button
            "Running Test..."      # Update button text
        )

    elif trigger == "internet-interval" and is_running:
        # Check if test is complete
        if speed_test_thread and not speed_test_thread.is_alive() and speed_test_result:
            result = speed_test_result
            speed_test_result = None  # Clear result

            if result.get("success"):
                results_display = [
                    html.H4("Speed Test Results", style={"color": "#00ccff", "textAlign": "center", "marginBottom": "20px"}),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Div("DOWNLOAD", style={"fontSize": "12px", "fontWeight": "bold", "color": "#00ccff", "marginBottom": "5px"}),
                                    html.Div(f"{result['download']}", style={"fontSize": "28px", "fontWeight": "bold", "color": "#00ccff"}),
                                    html.Div("Mbps", style={"fontSize": "12px", "color": "#ffffff"}),
                                ],
                                style={"textAlign": "center", "flex": "1"}
                            ),
                            html.Div(
                                [
                                    html.Div("UPLOAD", style={"fontSize": "12px", "fontWeight": "bold", "color": "#ffcc00", "marginBottom": "5px"}),
                                    html.Div(f"{result['upload']}", style={"fontSize": "28px", "fontWeight": "bold", "color": "#ffcc00"}),
                                    html.Div("Mbps", style={"fontSize": "12px", "color": "#ffffff"}),
                                ],
                                style={"textAlign": "center", "flex": "1"}
                            ),
                            html.Div(
                                [
                                    html.Div("PING", style={"fontSize": "12px", "fontWeight": "bold", "color": "#66ff66", "marginBottom": "5px"}),
                                    html.Div(f"{result['ping']}", style={"fontSize": "28px", "fontWeight": "bold", "color": "#66ff66"}),
                                    html.Div("ms", style={"fontSize": "12px", "color": "#ffffff"}),
                                ],
                                style={"textAlign": "center", "flex": "1"}
                            ),
                        ],
                        style={
                            "display": "flex",
                            "justifyContent": "space-around",
                            "marginBottom": "15px",
                            "flexWrap": "wrap"
                        }
                    ),
                    html.Div(
                        f"Server: {result['server']} • {result['location']}",
                        style={"textAlign": "center", "fontSize": "12px", "color": "#cccccc"}
                    )
                ]
            else:
                results_display = [
                    html.H4("Speed Test Failed", style={"color": "#ff6666", "textAlign": "center"}),
                    html.Div(result.get("error", "Unknown error"), style={"color": "#ffffff", "textAlign": "center"})
                ]

            return (
                {"display": "none"},      # Hide visualization
                {"display": "block"},     # Show results
                results_display,          # Results content
                False,                    # Set running state to false
                False,                    # Enable button
                "Speed Test"              # Reset button text
            )

    return dash.no_update, dash.no_update, dash.no_update, dash.no_update, dash.no_update, dash.no_update

@app.callback(
    Output("filtered-data", "data"),
    [
        Input("interval-component", "n_intervals"),
        Input("date-range-dropdown", "value"),
    ],
)
def fetch_data(n_minute, date_range):
    # Use the single source of truth for DB path
    filtered = get_filtered_data(DB_PATH, date_range)
    return filtered

@app.callback(
    [
        Output("success-graph", "figure"),
        Output("latency-graph", "figure"),
        Output("packetloss-graph", "figure"),
        Output("log-table", "data"),
        Output("full-up-count", "children"),
        Output("partial-up-count", "children"),
        Output("down-count", "children"),
    ],
    [Input("filtered-data", "data"), Input("latency-metrics-checkbox", "value"), Input("is-compact", "data")],
)
def update_dashboard(filtered_data, selected_latency_metrics, is_compact):
    df = pd.DataFrame(filtered_data)

    # Convert timestamps back to datetime (they're strings after JSON serialization)
    # Need to preserve timezone information for correct filtering
    if not df.empty and "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        # Ensure timezone-aware timestamps for proper comparison
        if df["timestamp"].dt.tz is None:
            # If timezone-naive, localize to display timezone
            try:
                df["timestamp"] = df["timestamp"].dt.tz_localize(DISPLAY_TZ)
            except Exception:
                # If already has some timezone info, try converting
                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(DISPLAY_TZ)

    logger.debug("Update Dashboard Callback")
    logger.debug(f"Number of records: {len(df)}")
    if not df.empty:
        logger.debug(f"Timestamp range (display TZ {DISPLAY_TZ}): {df['timestamp'].min()} to {df['timestamp'].max()}")

    # Read power cycle events (read-only) and filter to match the data time range
    power_cycle_df = pd.DataFrame()
    try:
        with _connect_ro(DB_PATH) as conn:
            power_cycle_df = pd.read_sql_query("SELECT timestamp FROM power_cycle_events", conn)
        if not power_cycle_df.empty:
            power_cycle_df["timestamp"] = pd.to_datetime(power_cycle_df["timestamp"], format="mixed", utc=True)

            # Filter power cycle events to match the filtered data time range
            if not df.empty:
                # df timestamps are in display TZ, so convert power cycle timestamps for comparison
                power_cycle_df["timestamp"] = _to_display_tz(power_cycle_df["timestamp"])
                min_time = df["timestamp"].min()
                max_time = df["timestamp"].max()

                power_cycle_df = power_cycle_df[
                    (power_cycle_df["timestamp"] >= min_time) &
                    (power_cycle_df["timestamp"] <= max_time)
                ].copy()
            else:
                power_cycle_df["timestamp"] = _to_display_tz(power_cycle_df["timestamp"])

            if not power_cycle_df.empty:
                logger.debug(
                    f"Power cycle events: {power_cycle_df['timestamp'].min()} -> {power_cycle_df['timestamp'].max()} "
                    f"({len(power_cycle_df)} rows)"
                )
            else:
                logger.debug("No power cycle events in the database.")
        else:
            logger.debug("No power cycle events found in the database.")
    except Exception as e:
        logger.error(f"Failed to fetch power cycle events: {e}")

    if df.empty:
        # Return empty figs / counts
        return {}, {}, {}, [], "Fully Up: 0", "Partially Up: 0", "Down: 0"

    # Sort by timestamp (already in display TZ)
    df.sort_values("timestamp", inplace=True)

    # Determine compact mode based on viewport width
    is_compact = bool(is_compact) if is_compact is not None else False

    # Success graph
    success_fig = {
        "data": [
            {
                "x": df["timestamp"],
                "y": df["success"],
                "type": "scattergl",
                "mode": "lines",
                "name": "Success Rate (%)",
                "line": {"color": "#00ccff", "width": 2},
                "marker": {"size": 5, "symbol": "circle"},
            },
            {
                "x": power_cycle_df.get("timestamp", []),
                "y": [50] * len(power_cycle_df),  # midline markers
                "mode": "markers",
                "name": "NBN Power Cycle",
                "marker": {"color": "red", "size": 24, "symbol": "square"},
                "text": ["NBN Power Cycle Event"] * len(power_cycle_df),
                "hoverinfo": "text+x",
            },
        ],
        "layout": {
            "title": "Internet Connectivity Over Time",
            "yaxis": {
                "title": "Ping Response Success Rate (%)",
                "range": [0, 100],
                "color": "#ffffff",
                "automargin": True,
                "tickfont": {"size": 12 if not is_compact else 10},
            },
            "xaxis": {
                "title": f"Timestamp ({DISPLAY_TZ})",
                "color": "#ffffff",
                "type": "date",
                "tickformat": "%Y-%m-%d %H:%M:%S",
                "range": [df["timestamp"].min(), df["timestamp"].max()],
                "automargin": True,
                "tickangle": -45 if is_compact else 0,
                "tickfont": {"size": 12 if not is_compact else 10},
            },
            "plot_bgcolor": "#1e1e1e",
            "paper_bgcolor": "#1e1e1e",
            "font": {"color": "#ffffff"},
            "titlefont": {"color": "#00ccff"},
            # Legend outside above plot with small gap (no overlap with top y-axis)
            "legend": (
                {
                    "orientation": "h",
                    "x": 0.0,
                    "y": 1.04,
                    "xanchor": "left",
                    "yanchor": "bottom",
                    "font": {"size": 11},
                }
                if is_compact
                else {
                    "orientation": "h",
                    "x": 0.0,
                    "y": 1.02,
                    "xanchor": "left",
                    "yanchor": "bottom",
                }
            ),
            "hovermode": "closest",
            # Compact margins: small top (space above legend), more bottom for x-axis labels
            "margin": ({"l": 22, "r": 16, "t": 40, "b": 64} if is_compact else {"l": 60, "r": 30, "t": 60, "b": 60}),
        },
    }

    # Latency graph
    ABS_MAX_LAT = 500
    latency_traces = []
    if selected_latency_metrics:
        color_map = {
            "avg_latency_ms": "#ffcc00",
            "max_latency_ms": "#ff6666",
            "min_latency_ms": "#66ff66",
        }
        name_map = {
            "avg_latency_ms": "Avg Latency (ms)",
            "max_latency_ms": "Max Latency (ms)",
            "min_latency_ms": "Min Latency (ms)",
        }
        for metric in selected_latency_metrics:
            latency_traces.append(
                {
                    "x": df["timestamp"],
                    "y": df[metric],
                    "type": "scattergl",
                    "mode": "lines",
                    "name": name_map.get(metric, metric),
                    "line": {"color": color_map.get(metric, "#000000"), "width": 2},
                    "marker": {"size": 5, "symbol": "circle"},
                }
            )
        max_latency = pd.DataFrame(df[selected_latency_metrics]).max().max()
        latency_y = [0, min(float(max_latency) * 1.1, ABS_MAX_LAT)]
        latency_fig = {
            "data": latency_traces,
            "layout": {
                "title": "Latency Over Time",
                "yaxis": {
                    "title": "Latency (ms)",
                    "range": latency_y,
                    "color": "#ffffff",
                    "automargin": True,
                    "tickfont": {"size": 12 if not is_compact else 10},
                },
                "xaxis": {
                    "title": f"Timestamp ({DISPLAY_TZ})",
                    "color": "#ffffff",
                    "type": "date",
                    "tickformat": "%Y-%m-%d %H:%M:%S",
                    "range": [df["timestamp"].min(), df["timestamp"].max()],
                    "automargin": True,
                    "tickangle": -45 if is_compact else 0,
                    "tickfont": {"size": 12 if not is_compact else 10},
                },
                "plot_bgcolor": "#1e1e1e",
                "paper_bgcolor": "#1e1e1e",
                "font": {"color": "#ffffff"},
                "titlefont": {"color": "#ffcc00"},
                # Legend outside above plot with small gap (no overlap with top y-axis)
                "legend": (
                    {
                        "orientation": "h",
                        "x": 0.0,
                        "y": 1.04,
                        "xanchor": "left",
                        "yanchor": "bottom",
                        "font": {"size": 11},
                    }
                    if is_compact
                    else {
                        "orientation": "h",
                        "x": 0.0,
                        "y": 1.02,
                        "xanchor": "left",
                        "yanchor": "bottom",
                    }
                ),
                "hovermode": "closest",
                "margin": ({"l": 22, "r": 16, "t": 40, "b": 64} if is_compact else {"l": 60, "r": 30, "t": 60, "b": 60}),
            },
        }
    else:
        latency_fig = {
            "data": [],
            "layout": {
                "title": "Latency Over Time",
                "yaxis": {"title": "Latency (ms)", "range": [0, ABS_MAX_LAT], "color": "#ffffff"},
                "xaxis": {
                    "title": f"Timestamp ({DISPLAY_TZ})",
                    "color": "#ffffff",
                    "type": "date",
                    "tickformat": "%Y-%m-%d %H:%M:%S",
                    "range": [df["timestamp"].min(), df["timestamp"].max()],
                },
                "annotations": [
                    {
                        "text": "Please select at least one latency metric to display.",
                        "xref": "paper",
                        "yref": "paper",
                        "showarrow": False,
                        "font": {"size": 16, "color": "#ffffff"},
                    }
                ],
                "plot_bgcolor": "#1e1e1e",
                "paper_bgcolor": "#1e1e1e",
                "font": {"color": "#ffffff"},
                "titlefont": {"color": "#ffcc00"},
                "legend": {"orientation": "h", "x": 0, "y": -0.2},
                "hovermode": "closest",
            },
        }

    # Packet loss
    ABS_MAX_LOSS = 100
    MIN_LOSS_Y_MAX = 25  # ensure some headroom when values are near 0
    packetloss_y = calculate_y_range(df["packet_loss"], ABS_MAX_LOSS)
    loss_y_max = max(MIN_LOSS_Y_MAX, packetloss_y[1])
    packetloss_fig = {
        "data": [
            {
                "x": df["timestamp"],
                "y": df["packet_loss"],
                "type": "scattergl",
                "mode": "lines",
                "name": "Packet Loss (%)",
                "line": {"color": "#ff0000", "width": 2},
                "marker": {"size": 5, "symbol": "circle"},
            }
        ],
        "layout": {
            "title": "Packet Loss Over Time",
            "yaxis": {
                "title": "Packet Loss (%)",
                "range": [0, loss_y_max],
                "color": "#ffffff",
                "automargin": True,
                "tickfont": {"size": 12 if not is_compact else 10},
            },
            "xaxis": {
                "title": f"Timestamp ({DISPLAY_TZ})",
                "color": "#ffffff",
                "type": "date",
                "tickformat": "%Y-%m-%d %H:%M:%S",
                "range": [df["timestamp"].min(), df["timestamp"].max()],
                "automargin": True,
                "tickangle": -45 if is_compact else 0,
                "tickfont": {"size": 12 if not is_compact else 10},
            },
            "plot_bgcolor": "#1e1e1e",
            "paper_bgcolor": "#1e1e1e",
            "font": {"color": "#ffffff"},
            "titlefont": {"color": "#ff0000"},
            "hovermode": "closest",
            "margin": ({"l": 22, "r": 16, "t": 32, "b": 64} if is_compact else {"l": 60, "r": 30, "t": 60, "b": 60}),
        },
    }

    # Table + counts
    table_data = df.sort_values(by="timestamp", ascending=False).to_dict("records")
    full_up_count     = f"Fully Up: {df[df['success'] == 100].shape[0]}"
    partial_up_count  = f"Partially Up: {df[(df['success'] > 0) & (df['success'] < 100)].shape[0]}"
    down_count        = f"Down: {df[df['success'] == 0].shape[0]}"

    return success_fig, latency_fig, packetloss_fig, table_data, full_up_count, partial_up_count, down_count

@app.callback(
    Output("button-state-store", "data"),
    Output("power-cycle-start", "data"),
    Input("power-cycle-button", "n_clicks"),
    Input("internet-interval", "n_intervals"),
    State("button-state-store", "data"),
    State("power-cycle-start", "data"),
    prevent_initial_call=True,
)
def on_power_cycle(n_clicks, n_fast, state, started_at):
    ctx = dash.callback_context
    try:
        if ctx.triggered:
            source = ctx.triggered[0]["prop_id"].split(".")[0]
        else:
            source = None
    except Exception:
        source = None

    if source == "power-cycle-button" and (n_clicks or 0) > 0:
        # Start the override script asynchronously and mark processing
        try:
            script_path = str(BASE_DIR / "scripts" / "power_cycle_nbn_override.py")
            subprocess.Popen(["python3", script_path])
            return "processing", int(time.time())
        except Exception as e:
            logger.error(f"Failed to trigger manual power cycle: {e}")
            return state or "idle", started_at

    # Auto-reset after ~40s
    # TODO: could use log: 2025-08-30 07:55:10,532 - INFO - nbn smart plug has been turned back on after retry attempt 1.
    # to change button state
    try:
        if state == "processing" and started_at and (int(time.time()) - int(started_at) >= 40):
            return "idle", started_at
    except Exception:
        pass

    return dash.no_update, dash.no_update

@app.callback(
    Output("power-cycle-button", "style"),
    Output("power-cycle-button", "disabled"),
    Output("power-cycle-button", "children"),
    Input("button-state-store", "data"),
    Input("tapo-connection-status", "data"),
    State("power-cycle-button", "style"),
)
def update_button_style(state, tapo_status, current_style):
    tapo_connected = tapo_status.get("connected", False) if tapo_status else False
    current_style = current_style or {}
    if state == "processing":
        return {**current_style, "backgroundColor": "#ffcc00", "cursor": "not-allowed"}, True, "Restarting..."
    elif not tapo_connected:
        return {**current_style, "backgroundColor": "#808080", "cursor": "not-allowed"}, True, "Tapo Not Connected"
    else:
        return {**current_style, "backgroundColor": "#00ccff", "cursor": "pointer"}, False, "Restart NBN"

@app.callback(
    Output("internet-status", "children"),
    Output("internet-status", "style"),
    Input("internet-interval", "n_intervals"),
)
def update_internet_status_live(n):
    status_text, bg_color = check_live_internet_status_for_badge()
    logger.debug(f"Badge status: {status_text} color={bg_color}")
    return status_text, {
        "backgroundColor": bg_color,
        "color": "#FFFFFF" if bg_color == "#808080" else "#1e1e1e",
    }

# ---------- Client-side callbacks ----------

# Track viewport width on a fast cadence using the existing internet interval.
# This avoids server overhead and lets us conditionally compact graph layouts on small screens.
app.clientside_callback(
    """
    function(n, prev) {
        try {
            var w = window.innerWidth || 1200;
            var compact = w < 700;
            if (prev === compact) { return window.dash_clientside.no_update; }
            return compact;
        } catch(e) { return prev || false; }
    }
    """,
    Output("is-compact", "data"),
    Input("internet-interval", "n_intervals"),
    State("is-compact", "data"),
)

# Animate single progress bar during speed test
app.clientside_callback(
    """
    function(is_running) {
        if (is_running) {
            // Show bar with consistent styling and animation
            document.getElementById('speed-test-progress').style.display = 'block';
            document.getElementById('speed-test-progress').style.backgroundColor = '#00ccff';
            document.getElementById('speed-test-progress').classList.add('speed-test-loading');
        } else {
            // Hide bar and clear animation
            document.getElementById('speed-test-progress').style.display = 'none';
            document.getElementById('speed-test-progress').classList.remove('speed-test-loading');
        }

        return window.dash_clientside.no_update;
    }
    """,
    Output("speed-test-progress", "className"),
    Input("speed-test-running", "data"),
    prevent_initial_call=True
)

# ---------- Healthcheck ----------

@server.route("/health")
def health_check():
    return jsonify(status="ok"), 200

# ---------- Dev runner (unused under gunicorn) ----------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8050, debug=False)
