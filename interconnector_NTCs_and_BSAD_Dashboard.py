"""
Interconnector NTC & Traded Volume Dashboard
---------------------------------------------
Visualises live Net Transfer Capacity CSVs published by NESO's Data Portal
for GB electricity interconnectors, split out by Auction Type and by flow
direction, with restriction-reason colouring. Also supports an "All" view
summing every interconnector into a single GB-wide total, alongside traded
volume from the Interconnector Requirement & Auction Summary dataset.

All interconnector NTC datasets on NESO's portal share the same column
layout, so this single dashboard can drive any of them via the DATASETS
registry below — add a new interconnector by adding one line.

Run with:
  pip install -r requirements.txt
  streamlit run interconnector_dashboard.py
"""

import io
from datetime import datetime

import pandas as pd
import requests
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go

# ---------------------------------------------------------------------------
# Dataset registry — add new interconnectors here.
# Find the CSV url on the dataset's NESO Data Portal page (the "Download
# (CSV)" link for the live/current data file, not an "Archived" one).
# ---------------------------------------------------------------------------
DATASETS = {
    "ElecLink": {
        "csv_url": "https://api.neso.energy/datastore/dump/1f748d48-1daf-4f1a-a5f1-d581e86a2190",
        "source_page": "https://www.neso.energy/data-portal/eleclink",
    },
    "NemoLink": {
        "csv_url": "https://api.neso.energy/datastore/dump/7d43e2a0-1c22-4e05-895d-756bae210756",
        "source_page": "https://www.neso.energy/data-portal/nemolink",
    },
    "NSL": {
        "csv_url": "https://api.neso.energy/datastore/dump/9db51c01-922d-413d-8b67-3938fdc14bdb",
        "source_page": "https://www.neso.energy/data-portal/nsl",
    },
    "Viking Link": {
        "csv_url": "https://api.neso.energy/datastore/dump/f4ee9a34-1bb5-405e-b4a0-bacb193fb188",
        "source_page": "https://www.neso.energy/data-portal/viking",
    },
    "IFA2": {
        "csv_url": "https://api.neso.energy/datastore/dump/f9ff9381-6eb1-40cd-903b-ca7282b9f2a9",
        "source_page": "https://www.neso.energy/data-portal/ifa2",
    },
    "IFA": {
        "csv_url": "https://api.neso.energy/datastore/dump/9e539e05-e09c-4983-91fd-c766f03d0339",
        "source_page": "https://www.neso.energy/data-portal/ifa",
    },
}

ALL_LABEL = "All (sum of interconnectors)"

# Fallback capacity (MW) used to pad any hour where an interconnector has no
# reported NTC value, instead of padding with zero. To GB uses the value as
# given; From GB uses its negative (matching the sign convention elsewhere).
DEFAULT_NTC_CAPACITY = {
    "ElecLink": 1014,
    "NemoLink": 1000,
    "IFA": 1000,
    "IFA2": 2000,
    "NSL": 1400,
    "Viking Link": 1400,
}

# Each interconnector gets its own color family; within it, auction types are
# shaded from lightest (Day Ahead) to darkest (Intraday 3), used by the
# "Flow (MW) by Auction Type & Direction" overlaid area chart.
INTERCONNECTOR_COLOR_FAMILY = {
    "ElecLink": "Blues",
    "NemoLink": "Greens",
    "NSL": "Oranges",
    "Viking Link": "Purples",
    "IFA2": "Reds",
    "IFA": "YlOrBr",
    "BritNed": "Greys",  # only appears in the BSAD auction data, no NTC dataset of its own
}
AUCTION_SHADE_ORDER = ["Day Ahead", "Intraday 1", "Intraday 2", "Intraday 3"]

# The BSAD auction summary data abbreviates interconnector names differently
# to the NTC datasets — map those abbreviations to the full names used above
# so both charts can share the same color family per interconnector.
BSAD_ABBREV_TO_FULL_NAME = {
    "IFA1": "IFA",
    "BN": "BritNed",
    "NEMO": "NemoLink",
    "IFA2": "IFA2",
    "EL": "ElecLink",
    "VKL": "Viking Link",
}


def get_auction_shade_color(interconnector_name: str, auction_type: str) -> str:
    """Pick a shade from the interconnector's color family for this auction
    type — lightest for Day Ahead, darkest for Intraday 3."""
    family_name = INTERCONNECTOR_COLOR_FAMILY.get(interconnector_name, "Greys")
    palette = getattr(px.colors.sequential, family_name)
    if auction_type in AUCTION_SHADE_ORDER:
        idx_in_order = AUCTION_SHADE_ORDER.index(auction_type)
        n = len(AUCTION_SHADE_ORDER)
    else:
        idx_in_order, n = 0, 1
    lo, hi = 1, len(palette) - 1  # skip index 0, often near-white
    pos = round(lo + idx_in_order * (hi - lo) / (n - 1)) if n > 1 else (lo + hi) // 2
    return palette[pos]


def rgb_to_rgba(rgb_str: str, alpha: float) -> str:
    """Convert a 'rgb(r,g,b)' string to 'rgba(r,g,b,alpha)' for semi-transparent fills."""
    nums = rgb_str[rgb_str.find("(") + 1: rgb_str.find(")")]
    return f"rgba({nums},{alpha})"


def get_interconnector_reference_color(bsad_abbrev: str) -> str:
    """The color used for this interconnector's Day Ahead series in the Flow
    chart, looked up via its BSAD abbreviation — used to keep the BSAD volume
    chart's interconnector colors consistent with the Flow chart's."""
    full_name = BSAD_ABBREV_TO_FULL_NAME.get(bsad_abbrev, bsad_abbrev)
    return get_auction_shade_color(full_name, "Day Ahead")


def get_interconnector_border_color(bsad_abbrev: str) -> str:
    """A darker shade from the same color family, used as a bar outline so
    the (deliberately pale) Day Ahead fill color doesn't look washed out."""
    full_name = BSAD_ABBREV_TO_FULL_NAME.get(bsad_abbrev, bsad_abbrev)
    return get_auction_shade_color(full_name, "Intraday 3")


AUCTION_SUMMARY_CSV_URL = "https://api.neso.energy/datastore/dump/6a928369-bed3-445f-af8a-69cdb2cc5089"
AUCTION_SUMMARY_SOURCE_PAGE = "https://www.neso.energy/data-portal/interconnector-requirement-and-auction-summary-data"

AUCTION_VOLUME_COLS = [
    "IFA1 Volume", "BN Volume", "NEMO Volume", "IFA2 Volume", "EL Volume", "VKL Volume",
]
AUCTION_IGNORE_COLS = [
    "Published DateTime", "Notes", "Auction ID", "Auction Lot ID",
    "Qualified IC", "Bid Deadline", "Default Price",
]

# Known column names — shared across all these NESO interconnector datasets
COL_UPLOAD = "Data Upload Time (GMT)"
COL_AUCTION = "Auction Type"
COL_PERIOD = "Operational Period Start Date & Time (GMT)"
COL_TO_GB = "Flow (MW) To GB"
COL_FROM_GB = "Flow (MW) From GB"
COL_REASON_TO = "Reason For Restriction To GB"
COL_REASON_FROM = "Reason For Restriction From GB"

st.set_page_config(page_title="Interconnector NTC Dashboard", layout="wide")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner=False)
def load_data(url: str) -> pd.DataFrame:
    """Download and parse a live CSV. Cached for 5 minutes; the sidebar
    'Refresh now' button clears this cache on demand."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))
    df.columns = [c.strip() for c in df.columns]
    return df


def prepare_data(df: pd.DataFrame) -> pd.DataFrame:
    """Clean, dedupe, and reshape into long form: one row per series per period."""
    df = df.copy()

    df[COL_UPLOAD] = pd.to_datetime(df[COL_UPLOAD], errors="coerce")
    df[COL_PERIOD] = pd.to_datetime(df[COL_PERIOD], errors="coerce")

    # Each (Auction Type, Operational Period) can be revised by later uploads.
    # Keep only the most recent upload for each combination.
    df = df.sort_values(COL_UPLOAD)
    df = df.drop_duplicates(subset=[COL_AUCTION, COL_PERIOD], keep="last")

    # Make "From GB" flows negative
    df[COL_TO_GB] = pd.to_numeric(df[COL_TO_GB], errors="coerce")
    df[COL_FROM_GB] = pd.to_numeric(df[COL_FROM_GB], errors="coerce") * -1

    # Reshape to long form with one series per (Auction Type x direction),
    # keeping the relevant restriction-reason column and upload time alongside
    # each value (upload time is needed later to pick the "current" value
    # when summing across auction rounds for the "All" view)
    to_gb = df[[COL_UPLOAD, COL_AUCTION, COL_PERIOD, COL_TO_GB, COL_REASON_TO]].rename(
        columns={COL_TO_GB: "Value", COL_REASON_TO: "Reason"}
    )
    to_gb["Direction"] = "To GB"

    from_gb = df[[COL_UPLOAD, COL_AUCTION, COL_PERIOD, COL_FROM_GB, COL_REASON_FROM]].rename(
        columns={COL_FROM_GB: "Value", COL_REASON_FROM: "Reason"}
    )
    from_gb["Direction"] = "From GB"

    long_df = pd.concat([to_gb, from_gb], ignore_index=True)
    long_df["Series"] = long_df[COL_AUCTION] + " – " + long_df["Direction"]
    long_df = long_df.sort_values(COL_PERIOD)
    return long_df


def collapse_to_current(long_df: pd.DataFrame) -> pd.DataFrame:
    """For each (Period, Direction), keep only the single most-recently-uploaded
    row (i.e. across all Auction Type revisions, take the latest known value).
    This avoids double-counting the same hour's capacity across auction rounds
    when summing totals across interconnectors."""
    df = long_df.sort_values(COL_UPLOAD)
    return df.groupby([COL_PERIOD, "Direction"], as_index=False).tail(1)


def prepare_auction_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Clean the interconnector requirement & auction summary data and reshape
    the per-interconnector volume columns into long form, with volume signed
    positive for Buy (Flow From GB) and negative for Sell (Flow To GB)."""
    df = df.copy()
    df = df.drop(columns=[c for c in AUCTION_IGNORE_COLS if c in df.columns], errors="ignore")

    df["Start Time"] = pd.to_datetime(df["Start Time"], errors="coerce")
    if "End Time" in df.columns:
        df["End Time"] = pd.to_datetime(df["End Time"], errors="coerce")

    id_cols = [c for c in df.columns if c not in AUCTION_VOLUME_COLS]
    melted = df.melt(
        id_vars=id_cols, value_vars=AUCTION_VOLUME_COLS,
        var_name="Interconnector", value_name="Volume",
    )
    melted["Volume"] = pd.to_numeric(melted["Volume"], errors="coerce")
    melted = melted.dropna(subset=["Volume"])
    melted["Interconnector"] = melted["Interconnector"].str.replace(" Volume", "", regex=False)

    buy_sell_norm = melted["Buy Sell"].astype(str).str.strip().str.lower()
    melted["Signed Volume"] = melted["Volume"].where(buy_sell_norm == "buy", -melted["Volume"])

    return melted


def prepare_bsad_cost(df: pd.DataFrame) -> pd.DataFrame:
    """Compute per-lot BSAD cost: sum the six interconnector volume columns for
    that lot, multiply by that lot's VWA Price, tagged by Buy/Sell."""
    df = df.copy()
    df = df.drop(columns=[c for c in AUCTION_IGNORE_COLS if c in df.columns], errors="ignore")

    df["Start Time"] = pd.to_datetime(df["Start Time"], errors="coerce")
    df["VWA Price"] = pd.to_numeric(df["VWA Price"], errors="coerce")
    for c in AUCTION_VOLUME_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["Total Volume"] = df[AUCTION_VOLUME_COLS].fillna(0).sum(axis=1)
    df["Buy Sell"] = df["Buy Sell"].astype(str).str.strip().str.capitalize()
    signed_volume = df["Total Volume"].where(df["Buy Sell"] == "Buy", -df["Total Volume"])
    df["Cost"] = signed_volume * df["VWA Price"]

    df = df.dropna(subset=["Start Time", "Cost"])
    return df[["Start Time", "Buy Sell", "Total Volume", "VWA Price", "Cost"]]


REASON_COLORS = {
    "No Restriction": "#9CA3AF",       # gray
    "Network Constraints": "#F59E0B",  # amber
    "Margin Extremes": "#EF4444",      # red
}
FALLBACK_PALETTE = px.colors.qualitative.Set2


def reason_color(reason: str, extra_colors: dict) -> str:
    if reason in REASON_COLORS:
        return REASON_COLORS[reason]
    if reason not in extra_colors:
        extra_colors[reason] = FALLBACK_PALETTE[len(extra_colors) % len(FALLBACK_PALETTE)]
    return extra_colors[reason]


def build_reason_colored_figure(series_df: pd.DataFrame, series_name: str):
    """Build a line chart for a single series where each segment is colored by
    the restriction reason in effect at that point in time. Segments share a
    boundary point with their neighbours so the line stays visually continuous."""
    sub = series_df.sort_values(COL_PERIOD).reset_index(drop=True)
    if sub.empty:
        return None

    block_id = (sub["Reason"] != sub["Reason"].shift()).cumsum()
    fig = px.line()
    seen_reasons = set()
    extra_colors: dict = {}

    for bid in block_id.unique():
        idx = sub.index[block_id == bid]
        start_idx, end_idx = idx[0], idx[-1]
        reason_val = sub.loc[idx[0], "Reason"]
        if reason_val == "No Data":
            # Don't render a trace for genuine gaps — the neighbouring real
            # blocks already extend one point into the gap (below), which is
            # enough for their own lines to stop cleanly instead of drawing a
            # diagonal across the missing stretch.
            continue
        seg_start = max(start_idx - 1, 0)
        seg_end = min(end_idx + 1, len(sub) - 1)
        seg = sub.iloc[seg_start: seg_end + 1]
        color = reason_color(reason_val, extra_colors)

        fig.add_scatter(
            x=seg[COL_PERIOD], y=seg["Value"],
            mode="lines",
            line=dict(color=color, width=2),
            name=reason_val,
            legendgroup=reason_val,
            showlegend=reason_val not in seen_reasons,
            hovertemplate=f"%{{x}}<br>%{{y}} MW<br>Reason: {reason_val}<extra></extra>",
        )
        seen_reasons.add(reason_val)

    fig.add_hline(y=0, line_width=1, line_color="gray")
    fig.update_layout(
        title=series_name,
        xaxis_title="Operational Period (GMT)",
        yaxis_title="Flow (MW)",
        legend_title="Reason for restriction",
        margin=dict(l=10, r=10, t=40, b=10),
    )
    return fig


def reindex_series_to_hourly_grid(df: pd.DataFrame, date_range) -> pd.DataFrame:
    """Reindex each Series to a full hourly grid across date_range, leaving
    genuinely missing hours as NaN. Auction rounds like Intraday 1/2/3 only
    apply to periods near real-time, so most hours have no row for them —
    without this, a line chart would draw a long diagonal straight through
    the gap between the two nearest real points. NaN breaks the line instead."""
    full_periods = pd.date_range(date_range[0], date_range[1], freq="h")
    series_list = df["Series"].unique()
    full_index = pd.MultiIndex.from_product([full_periods, series_list], names=[COL_PERIOD, "Series"])
    grid = pd.DataFrame(index=full_index).reset_index()
    return grid.merge(df[[COL_PERIOD, "Series", "Value"]], on=[COL_PERIOD, "Series"], how="left")


def reindex_single_series_to_hourly_grid(series_df: pd.DataFrame, date_range) -> pd.DataFrame:
    """Same idea as reindex_series_to_hourly_grid but for a single series
    already filtered down, keeping Reason so the restriction-colored chart
    can render. Missing hours get a placeholder Reason so a long gap collapses
    into one contiguous block rather than one tiny block per missing hour."""
    full_periods = pd.date_range(date_range[0], date_range[1], freq="h")
    grid = pd.DataFrame({COL_PERIOD: full_periods})
    merged = grid.merge(series_df[[COL_PERIOD, "Value", "Reason"]], on=COL_PERIOD, how="left")
    merged["Reason"] = merged["Reason"].fillna("No Data")
    return merged


def split_into_contiguous_blocks(df: pd.DataFrame, value_col: str = "Value") -> list:
    """Split a gridded series into separate contiguous runs of real (non-NaN)
    data. Needed because while a plain line breaks cleanly at NaN, an area
    fill (fill='tozeroy') still draws a straight diagonal connecting across
    a NaN gap — rendering each real run as its own trace avoids that."""
    is_real = df[value_col].notna()
    block_id = (is_real != is_real.shift()).cumsum()
    return [g for _, g in df.groupby(block_id) if g[value_col].notna().all() and not g.empty]


def default_period_range(min_date: pd.Timestamp, max_date: pd.Timestamp):
    """7 days ago through the end of tomorrow, clamped to the data's range."""
    today = pd.Timestamp.now().normalize()
    default_start = today - pd.Timedelta(days=7)
    default_end = today + pd.Timedelta(days=2) - pd.Timedelta(seconds=1)
    default_start = max(default_start, min_date)
    default_end = min(default_end, max_date)
    if default_start > default_end:
        default_start, default_end = min_date, max_date
    return default_start, default_end


def period_range_control(min_date: pd.Timestamp, max_date: pd.Timestamp, key_prefix: str):
    """Renders a slider plus two synced date-input boxes (start/end) in the
    sidebar for picking a period range, and returns the current
    (start_datetime, end_datetime) tuple. Editing either the slider or the
    date boxes keeps the other in sync."""
    slider_key = f"{key_prefix}_period_slider"
    start_key = f"{key_prefix}_period_start_date"
    end_key = f"{key_prefix}_period_end_date"

    min_dt = min_date.to_pydatetime()
    max_dt = max_date.to_pydatetime()

    if slider_key not in st.session_state:
        default_start, default_end = default_period_range(min_date, max_date)
        st.session_state[slider_key] = (default_start.to_pydatetime(), default_end.to_pydatetime())
    else:
        # The available date range can shrink (e.g. narrowing the
        # Interconnectors/Auction type filters), leaving a previously-picked
        # start/end outside the new bounds. Clamp before any widget renders,
        # since date_input/slider raise an error if their stored value falls
        # outside min_value/max_value.
        s, e = st.session_state[slider_key]
        s = min(max(s, min_dt), max_dt)
        e = min(max(e, min_dt), max_dt)
        if s > e:
            s, e = min_dt, max_dt
        st.session_state[slider_key] = (s, e)

    # Keep the date-input boxes in sync with whatever the slider value ended
    # up being (freshly defaulted, clamped, or unchanged)
    s, e = st.session_state[slider_key]
    st.session_state[start_key] = s.date()
    st.session_state[end_key] = e.date()

    def _sync_from_dates():
        s = st.session_state[start_key]
        e = st.session_state[end_key]
        if s > e:
            s, e = e, s
        start_dt = datetime.combine(s, datetime.min.time())
        end_dt = datetime.combine(e, datetime.max.time().replace(microsecond=0))
        # Clamp to the actual data range so we never exceed the slider's bounds
        start_dt = max(start_dt, min_dt)
        end_dt = min(end_dt, max_dt)
        if start_dt > end_dt:
            start_dt, end_dt = min_dt, max_dt
        st.session_state[slider_key] = (start_dt, end_dt)

    def _sync_from_slider():
        s, e = st.session_state[slider_key]
        st.session_state[start_key] = s.date()
        st.session_state[end_key] = e.date()

    st.sidebar.date_input(
        "Period start date", key=start_key,
        min_value=min_date.date(), max_value=max_date.date(),
        on_change=_sync_from_dates,
    )
    st.sidebar.date_input(
        "Period end date", key=end_key,
        min_value=min_date.date(), max_value=max_date.date(),
        on_change=_sync_from_dates,
    )
    st.sidebar.slider(
        "Period range",
        min_value=min_date.to_pydatetime(), max_value=max_date.to_pydatetime(),
        key=slider_key, on_change=_sync_from_slider,
    )

    return st.session_state[slider_key]


# ---------------------------------------------------------------------------
# Sidebar controls
# ---------------------------------------------------------------------------
st.sidebar.header("Settings")

dataset_name = st.sidebar.selectbox(
    "Interconnector", options=[ALL_LABEL] + list(DATASETS.keys())
)

if st.sidebar.button("Refresh now"):
    st.cache_data.clear()
    st.rerun()


# ===========================================================================
# ALL INTERCONNECTORS — summed view
# ===========================================================================
if dataset_name == ALL_LABEL:
    st.title("NTC and Traded IC BSAD Dashboard")

    st.sidebar.divider()
    st.sidebar.subheader("Filters")

    default_interconnectors = [
        name for name in ["ElecLink", "NemoLink", "NSL", "Viking Link"] if name in DATASETS
    ]
    selected_interconnectors = st.sidebar.multiselect(
        "Interconnectors", options=list(DATASETS.keys()), default=default_interconnectors
    )

    if not selected_interconnectors:
        st.info("Select at least one interconnector.")
        st.stop()

    per_interconnector_raw = {}
    fetch_errors = []
    for name in selected_interconnectors:
        cfg = DATASETS[name]
        try:
            with st.spinner(f"Fetching {name}..."):
                raw = load_data(cfg["csv_url"])
            per_interconnector_raw[name] = prepare_data(raw)
        except Exception as e:
            fetch_errors.append(f"{name}: {e}")

    if fetch_errors:
        st.warning("Some interconnectors failed to load: " + "; ".join(fetch_errors))

    if not per_interconnector_raw:
        st.error("Couldn't fetch data from any selected interconnector.")
        st.stop()

    # Union of auction types available across the selected interconnectors
    all_auction_types = sorted(set().union(
        *[set(df[COL_AUCTION].dropna().unique()) for df in per_interconnector_raw.values()]
    ))

    default_auctions = [a for a in ["Day Ahead"] if a in all_auction_types] or all_auction_types
    selected_auctions = st.sidebar.multiselect(
        "Auction type", options=all_auction_types, default=default_auctions
    )

    directions = ["To GB", "From GB"]
    selected_directions = st.sidebar.multiselect(
        "Direction", options=directions, default=directions
    )

    if not selected_auctions:
        st.info("Select at least one auction type.")
        st.stop()

    per_interconnector = []
    for name, long_df in per_interconnector_raw.items():
        # Restrict to selected auction types, THEN collapse to the most
        # recently revised value per period/direction among those types only —
        # this avoids double-counting the same hour across multiple rounds.
        restricted = long_df[long_df[COL_AUCTION].isin(selected_auctions)]
        current = collapse_to_current(restricted).copy()
        current["Interconnector"] = name
        per_interconnector.append(current)

    combined_current = pd.concat(per_interconnector, ignore_index=True)

    st.caption(
        f"Last fetched: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · "
        f"{len(per_interconnector_raw)}/{len(selected_interconnectors)} selected interconnectors loaded · "
        f"each contributes its most recently revised value (among selected auction types) per hour"
    )

    min_date = combined_current[COL_PERIOD].min()
    max_date = combined_current[COL_PERIOD].max()
    date_range = period_range_control(min_date, max_date, key_prefix="all")

    filtered_current = combined_current[
        combined_current["Direction"].isin(selected_directions)
        & combined_current[COL_PERIOD].between(date_range[0], date_range[1])
    ]

    # Zero-fill every (period, interconnector, direction) combination across
    # the full hourly grid — otherwise any interconnector with a gap or
    # shorter history in its raw NTC data quietly drags the total down for
    # those hours instead of contributing a proper zero, which is what was
    # causing the total to drift from the fully-gridded BSAD charts.
    full_periods_ntc = pd.date_range(date_range[0], date_range[1], freq="h")
    full_index_ntc = pd.MultiIndex.from_product(
        [full_periods_ntc, selected_interconnectors, selected_directions],
        names=[COL_PERIOD, "Interconnector", "Direction"],
    )
    filtered_current_full = (
        pd.DataFrame(index=full_index_ntc).reset_index()
        .merge(
            filtered_current[[COL_PERIOD, "Interconnector", "Direction", "Value"]],
            on=[COL_PERIOD, "Interconnector", "Direction"], how="left",
        )
    )
    default_value = filtered_current_full["Interconnector"].map(DEFAULT_NTC_CAPACITY)
    default_value = default_value.where(filtered_current_full["Direction"] == "To GB", -default_value)
    filtered_current_full["Value"] = filtered_current_full["Value"].fillna(default_value)

    totals = (
        filtered_current_full.groupby([COL_PERIOD, "Direction"], as_index=False)["Value"]
        .sum()
    )
    totals["Series"] = "Total " + totals["Direction"]

    st.subheader("Total NTCs across selected interconnectors")
    if not totals.empty:
        fig = px.line(
            totals, x=COL_PERIOD, y="Value", color="Series",
            labels={COL_PERIOD: "Operational Period (GMT)", "Value": "Flow (MW)"},
        )
        fig.add_hline(y=0, line_width=1, line_color="gray")
        fig.update_layout(
            margin=dict(l=10, r=10, t=30, b=80),
            legend=dict(orientation="h", yanchor="top", y=-0.25, xanchor="center", x=0.5),
        )
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Any interconnector with no data for a given hour contributes its default "
            "capacity (e.g. 1014 MW for ElecLink, 1000 MW for NemoLink/IFA, 2000 MW for "
            "IFA2, 1400 MW for NSL/Viking Link; negative for From GB) rather than zero."
        )
    else:
        st.info("No data matches the current filters.")

    st.info(
        "Restriction-reason breakdown isn't shown in the combined 'All' view, since "
        "reasons differ per interconnector. Select a single interconnector to see it."
    )

    # -----------------------------------------------------------------------
    # Traded volume (Interconnector Requirement & Auction Summary data)
    # -----------------------------------------------------------------------
    st.subheader("BSAD volume by interconnector")
    try:
        with st.spinner("Fetching auction summary data..."):
            auction_raw = load_data(AUCTION_SUMMARY_CSV_URL)
        auction_long = prepare_auction_summary(auction_raw)

        auction_filtered = auction_long[
            auction_long["Start Time"].between(date_range[0], date_range[1])
        ]
        auction_agg = (
            auction_filtered.groupby(["Start Time", "Interconnector"], as_index=False)
            ["Signed Volume"].sum()
        )

        # Zero-fill any hour with no traded volume (for a given interconnector)
        # so this chart covers the same full period range as the other charts,
        # rather than stopping wherever the real trade data happens to end.
        full_periods = pd.date_range(date_range[0], date_range[1], freq="h")
        full_index = pd.MultiIndex.from_product(
            [full_periods, AUCTION_VOLUME_COLS], names=["Start Time", "Interconnector"]
        )
        auction_agg = (
            pd.DataFrame(index=full_index).reset_index()
            .assign(Interconnector=lambda d: d["Interconnector"].str.replace(" Volume", "", regex=False))
            .merge(auction_agg, on=["Start Time", "Interconnector"], how="left")
        )
        auction_agg["Signed Volume"] = auction_agg["Signed Volume"].fillna(0)

        if not auction_agg.empty:
            bsad_color_map = {
                abbrev: get_interconnector_reference_color(abbrev)
                for abbrev in auction_agg["Interconnector"].unique()
            }
            vol_fig = px.bar(
                auction_agg, x="Start Time", y="Signed Volume", color="Interconnector",
                barmode="relative", color_discrete_map=bsad_color_map,
                labels={"Start Time": "Period start (GMT)", "Signed Volume": "Volume (MW)"},
            )
            vol_fig.add_hline(y=0, line_width=1, line_color="gray")
            vol_fig.for_each_trace(lambda t: t.update(
                opacity=1,
                marker_line_color=get_interconnector_border_color(t.name),
                marker_line_width=1,
            ))
            vol_fig.update_layout(
                margin=dict(l=10, r=10, t=30, b=80),
                legend=dict(orientation="h", yanchor="top", y=-0.25, xanchor="center", x=0.5),
            )
            st.plotly_chart(vol_fig, use_container_width=True)
            st.caption(
                "Positive = Buy (Flow From GB), negative = Sell (Flow To GB); "
                "hours with no trades show as 0. "
                f"[Source page]({AUCTION_SUMMARY_SOURCE_PAGE})"
            )
        else:
            st.info("No traded volume data in the current period range.")
    except Exception as e:
        st.error(f"Couldn't fetch auction summary data: {e}")

    # -----------------------------------------------------------------------
    # VWA Price
    # -----------------------------------------------------------------------
    st.subheader("Volume-Weighted Average (VWA) IC BSAD Price")
    try:
        price_df = load_data(AUCTION_SUMMARY_CSV_URL).copy()
        price_df["Start Time"] = pd.to_datetime(price_df["Start Time"], errors="coerce")
        price_df["VWA Price"] = pd.to_numeric(price_df["VWA Price"], errors="coerce")
        price_df["Buy Sell"] = price_df["Buy Sell"].astype(str).str.strip().str.capitalize()
        price_df = price_df.dropna(subset=["VWA Price", "Start Time"])

        price_filtered = price_df[
            price_df["Start Time"].between(date_range[0], date_range[1])
        ]
        price_agg = (
            price_filtered.groupby(["Start Time", "Buy Sell"], as_index=False)["VWA Price"]
            .mean()
        )

        # Zero-fill any hour with no trades (of a given Buy/Sell type) rather
        # than leaving a gap in the line
        full_periods = pd.date_range(date_range[0], date_range[1], freq="h")
        full_index = pd.MultiIndex.from_product(
            [full_periods, ["Buy", "Sell"]], names=["Start Time", "Buy Sell"]
        )
        price_agg = (
            pd.DataFrame(index=full_index).reset_index()
            .merge(price_agg, on=["Start Time", "Buy Sell"], how="left")
        )
        price_agg["VWA Price"] = price_agg["VWA Price"].fillna(0)
        # Negate Sell prices so this chart diverges above/below zero the same
        # way the volume and cost charts do
        price_agg["VWA Price"] = price_agg["VWA Price"].where(
            price_agg["Buy Sell"] == "Buy", -price_agg["VWA Price"]
        )
        price_agg["Series"] = price_agg["Buy Sell"] + " VWA Price"

        if not price_agg.empty:
            price_fig = px.line(
                price_agg, x="Start Time", y="VWA Price", color="Series",
                labels={"Start Time": "Period start (GMT)", "VWA Price": "£/MWh"},
            )
            price_fig.add_hline(y=0, line_width=1, line_color="gray")
            price_fig.update_layout(
                margin=dict(l=10, r=10, t=30, b=80),
                legend=dict(orientation="h", yanchor="top", y=-0.25, xanchor="center", x=0.5),
            )
            st.plotly_chart(price_fig, use_container_width=True)
            st.caption(
                "Averaged across auction lots where more than one applies to the same hour; "
                "hours with no trades of that type show as 0. Positive = Buy, negative = Sell "
                "(mirroring the volume and cost charts). "
                f"[Source page]({AUCTION_SUMMARY_SOURCE_PAGE})"
            )
        else:
            st.info("No VWA price data in the current period range.")
    except Exception as e:
        st.error(f"Couldn't build VWA price chart: {e}")

    # -----------------------------------------------------------------------
    # Total BSAD costs
    # -----------------------------------------------------------------------
    st.subheader("Total IC BSAD costs")
    try:
        cost_df = prepare_bsad_cost(load_data(AUCTION_SUMMARY_CSV_URL))

        cost_filtered = cost_df[
            cost_df["Start Time"].between(date_range[0], date_range[1])
        ]
        cost_agg = (
            cost_filtered.groupby(["Start Time", "Buy Sell"], as_index=False)["Cost"]
            .sum()
        )

        # Zero-fill any hour with no cost (of a given Buy/Sell type)
        full_periods = pd.date_range(date_range[0], date_range[1], freq="h")
        full_index = pd.MultiIndex.from_product(
            [full_periods, ["Buy", "Sell"]], names=["Start Time", "Buy Sell"]
        )
        cost_agg = (
            pd.DataFrame(index=full_index).reset_index()
            .merge(cost_agg, on=["Start Time", "Buy Sell"], how="left")
        )
        cost_agg["Cost"] = cost_agg["Cost"].fillna(0)

        if not cost_agg.empty:
            cost_fig = px.bar(
                cost_agg, x="Start Time", y="Cost", color="Buy Sell",
                barmode="relative",
                labels={"Start Time": "Period start (GMT)", "Cost": "Cost (£)"},
            )
            cost_fig.add_hline(y=0, line_width=1, line_color="gray")
            cost_fig.update_layout(
                margin=dict(l=10, r=10, t=30, b=80),
                legend=dict(orientation="h", yanchor="top", y=-0.25, xanchor="center", x=0.5),
            )
            st.plotly_chart(cost_fig, use_container_width=True)
            st.caption(
                "Cost = (sum of IFA1/BN/NEMO/IFA2/EL/VKL Volume) × VWA Price per auction lot, "
                "summed per hour. Positive = Buy, negative = Sell (mirroring the volume chart); "
                "hours with no trades show as 0. "
                f"[Source page]({AUCTION_SUMMARY_SOURCE_PAGE})"
            )
        else:
            st.info("No cost data in the current period range.")
    except Exception as e:
        st.error(f"Couldn't build BSAD cost chart: {e}")

    st.subheader("Latest total values")
    if not totals.empty:
        latest_totals = totals.sort_values(COL_PERIOD).groupby("Series").tail(1)
        metric_cols = st.columns(min(len(latest_totals), 6) or 1)
        for i, (_, row) in enumerate(latest_totals.iterrows()):
            with metric_cols[i % len(metric_cols)]:
                st.metric(row["Series"], f"{row['Value']:,.0f} MW")

    st.subheader("Latest value by interconnector")
    if not filtered_current.empty:
        latest_by_ic = (
            filtered_current.sort_values(COL_PERIOD)
            .groupby(["Interconnector", "Direction"])
            .tail(1)
            .sort_values(["Direction", "Interconnector"])
        )
        st.dataframe(
            latest_by_ic[["Interconnector", "Direction", COL_PERIOD, "Value"]],
            use_container_width=True, hide_index=True,
        )

    with st.expander("Raw data (current value per interconnector, among selected auction types)"):
        st.dataframe(filtered_current, use_container_width=True, height=400)
        st.download_button(
            "Download CSV",
            data=filtered_current.to_csv(index=False).encode("utf-8"),
            file_name="all_interconnectors_ntc_current.csv",
            mime="text/csv",
        )

    st.stop()


# ===========================================================================
# SINGLE INTERCONNECTOR view
# ===========================================================================
dataset_cfg = DATASETS[dataset_name]
st.sidebar.markdown(f"[Source page]({dataset_cfg['source_page']})")

st.title(f"{dataset_name} Net Transfer Capacity — Live Dashboard")

try:
    with st.spinner("Fetching latest data..."):
        raw_df = load_data(dataset_cfg["csv_url"])
except Exception as e:
    st.error(f"Couldn't fetch data from NESO: {e}")
    st.stop()

if raw_df.empty:
    st.warning("The data source returned no rows.")
    st.stop()

long_df = prepare_data(raw_df)

st.caption(
    f"Last fetched: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · "
    f"{len(raw_df):,} raw rows · {raw_df[COL_UPLOAD].max()} most recent upload (GMT)"
)

# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
st.sidebar.divider()
st.sidebar.subheader("Filters")

auction_types = sorted(long_df[COL_AUCTION].dropna().unique())
selected_auctions = st.sidebar.multiselect(
    "Auction type", options=auction_types, default=auction_types
)

directions = ["To GB", "From GB"]
selected_directions = st.sidebar.multiselect(
    "Direction", options=directions, default=directions
)

min_date = long_df[COL_PERIOD].min()
max_date = long_df[COL_PERIOD].max()
date_range = period_range_control(min_date, max_date, key_prefix="single")

filtered = long_df[
    long_df[COL_AUCTION].isin(selected_auctions)
    & long_df["Direction"].isin(selected_directions)
    & long_df[COL_PERIOD].between(date_range[0], date_range[1])
]

st.subheader("Flow (MW) by Auction Type & Direction")
if not filtered.empty:
    filtered_gridded = reindex_series_to_hourly_grid(filtered, date_range)

    direction_order = ["To GB", "From GB"]
    fig = go.Figure()
    legend_shown = set()

    for auction in AUCTION_SHADE_ORDER:
        if auction not in selected_auctions:
            continue
        color = get_auction_shade_color(dataset_name, auction)
        fillcolor = rgb_to_rgba(color, 0.45)
        for direction in direction_order:
            series_name = f"{auction} – {direction}"
            sub = filtered_gridded[filtered_gridded["Series"] == series_name]
            if sub.empty or sub["Value"].isna().all():
                continue
            show_legend = auction not in legend_shown
            for block in split_into_contiguous_blocks(sub):
                if len(block) < 2:
                    continue  # a single isolated point can't form a visible area
                fig.add_scatter(
                    x=block[COL_PERIOD], y=block["Value"],
                    mode="lines",
                    line=dict(color=color, width=1.5),
                    fill="tozeroy",
                    fillcolor=fillcolor,
                    name=auction,
                    legendgroup=auction,
                    showlegend=show_legend,
                    hovertemplate=f"%{{x}}<br>%{{y}} MW<br>{series_name}<extra></extra>",
                )
                show_legend = False
            legend_shown.add(auction)

    fig.add_hline(y=0, line_width=1, line_color="gray")
    fig.update_layout(
        xaxis_title="Operational Period (GMT)",
        yaxis_title="Flow (MW)",
        margin=dict(l=10, r=10, t=30, b=80),
        legend=dict(orientation="h", yanchor="top", y=-0.25, xanchor="center", x=0.5, title="Auction type"),
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        "Overlaid (not stacked), semi-transparent by auction type — lighter shades are "
        "closer to Day Ahead, darker shades are closer to Intraday 3. To GB sits above "
        "zero, From GB below."
    )
else:
    st.info("No data matches the current filters.")

# ---------------------------------------------------------------------------
# Reason-colored single-series view
# ---------------------------------------------------------------------------
st.subheader("Restriction reason breakdown")
available_series = sorted(filtered["Series"].unique())

if available_series:
    chosen_series = st.selectbox("Series to inspect", options=available_series)
    series_df = filtered[filtered["Series"] == chosen_series]
    series_df_gridded = reindex_single_series_to_hourly_grid(series_df, date_range)
    reason_fig = build_reason_colored_figure(series_df_gridded, chosen_series)
    if reason_fig is not None:
        st.plotly_chart(reason_fig, use_container_width=True)
    else:
        st.info("No data for this series in the current filters.")
else:
    st.info("No data matches the current filters.")

# ---------------------------------------------------------------------------
# Latest values
# ---------------------------------------------------------------------------
st.subheader("Latest values per series")
if not filtered.empty:
    latest_per_series = (
        filtered.sort_values(COL_PERIOD).groupby("Series").tail(1).sort_values("Series")
    )
    metric_cols = st.columns(min(len(latest_per_series), 6) or 1)
    for i, (_, row) in enumerate(latest_per_series.iterrows()):
        with metric_cols[i % len(metric_cols)]:
            st.metric(row["Series"], f"{row['Value']:,.0f} MW")

# ---------------------------------------------------------------------------
# Raw data
# ---------------------------------------------------------------------------
with st.expander("Raw data (post-cleaning, long format)"):
    st.dataframe(filtered, use_container_width=True, height=400)
    st.download_button(
        "Download CSV",
        data=filtered.to_csv(index=False).encode("utf-8"),
        file_name=f"{dataset_name.lower()}_ntc_long.csv",
        mime="text/csv",
    )
