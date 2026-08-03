"""Render a bundle's Wi-Fi context snapshots (``context/**.json``) as PNGs.

A downloaded session ZIP carries the connect-time context captures as JSON
(lossless) plus CSV (spreadsheet-friendly). Neither is readable at a glance:
the operator who opens the ZIP a month later wants the same three views the
live UI shows — the per-band channel-usage chart, the SSID capability table,
and the associated-clients table. This tool renders exactly those, offline,
from files already in the bundle.

Inputs (written by ``app/services/context_snapshot.py`` and
``app/services/survey_snapshot.py``, selected into the bundle by
``bundle_context``)::

    context/site-survey/site-survey-<dut>-<YYYYmmdd-HHMMSS>.json
    context/ssid-capability/ssid-capability-<dut>-<YYYYmmdd-HHMMSS>.json
    context/wifi-clients/wifi-clients-<dut>-<YYYYmmdd-HHMMSS>.json

Outputs, in the session directory root, prefixed exactly like analyzer3.py's
(``<mmddHHMM>_<time_tag>_<fw_tag>_``, contract §5)::

    {prefix}survey_channels_2g4.png / _5g.png / _6g.png
    {prefix}ssid_capability.png
    {prefix}wifi_clients_table.png

Contract notes (``tools/CONTRACT_wifi_context.md``):

* §4 — invoked as a subprocess with ``cwd`` = the session directory; no args
  required (an optional positional session dir exists for standalone runs).
  The caller supplies ``MPLBACKEND=Agg`` / ``MPLCONFIGDIR``.
* §4 — the prefix helpers below are **copied** from ``analyzer3.py``, not
  imported: that module has no ``__main__`` guard, so importing it runs the
  whole analyzer. The duplication is deliberate and recorded in the contract.
* §7 — no usable input writes **nothing at all**: no placeholder PNG, no
  empty table, no zero-bar chart standing in for a measurement that never
  happened. Every skip says why on stdout and the exit status stays 0.

Bar heights are the **raw neighbour (SSID) count per channel**, matching what
``SiteSurveyCard.tsx`` draws — deliberately *not* the recommendation's
signal-weighted ``occupancy`` score, which is a different number (adjacent
channel bleed on 2.4 GHz, signal weighting everywhere). The occupancy score is
shown as text alongside the recommendation so both readings survive into the
PNG without being conflated.

The module is split into pure data-prep functions (``prepare_*``, testable
without a renderer) and a thin matplotlib layer (``render_*``); the tests
assert the prep output, never pixels.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import matplotlib

# The caller normally exports MPLBACKEND=Agg (contract §4). Standalone runs and
# the test suite may not, and this tool must never try to open a display.
if not os.environ.get("MPLBACKEND"):
    matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402 — must follow the backend selection
from matplotlib.patches import Patch  # noqa: E402

# ======================================================
#                 Version / Metadata
# ======================================================
VERSION_NAME = "context_render v1.0.0"

# ======================================================
#        Prefix helpers — COPIED from analyzer3.py:29-67
#   Prefix: <mmddHHMM>_<HHMMSS>_<FW>_   (contract §4/§5)
#   Do NOT replace these with `import analyzer3`.
# ======================================================
LOG_EXT = (".log", ".txt")


def extract_time_tag(filename: str) -> str:
    m = re.search(r"_(\d{6})(?:_|\.|$)", filename)
    return m.group(1) if m else "notime"


def fw_triplet_to_tag(maj: str, minor: str, patch: str) -> str:
    try:
        p = int(patch)
        return f"{int(maj)}{int(minor)}{p:03d}"  # 1.9.300 -> 19300
    except Exception:
        return f"{maj}{minor}{patch}"


def extract_fw_tag(filename: str) -> str:
    m = re.search(r"(?:^|[^0-9])v?(\d+)\.(\d+)\.(\d+)(?:[^0-9]|$)", filename, re.IGNORECASE)
    if m:
        return fw_triplet_to_tag(m.group(1), m.group(2), m.group(3))
    m2 = re.search(r"(?<!\d)(\d{5})(?!\d)", filename)
    if m2:
        return m2.group(1)
    return "nofw"


def output_prefix(session_dir: Path, now: datetime | None = None) -> str:
    """``<mmddHHMM>_<time_tag>_<fw_tag>_`` for this session directory.

    Tags come from the session log's filename exactly as in analyzer3.py
    (``MULTI`` when several logs disagree, ``_nofw_`` dropped). Unlike
    analyzer3 a log-less directory is not an error here — the context JSONs are
    renderable on their own — so it falls back to ``notime`` and no fw tag.
    """
    now = now or datetime.now()
    try:
        logs = sorted(p.name for p in session_dir.iterdir() if p.is_file() and p.name.endswith(LOG_EXT))
    except OSError:
        logs = []

    if logs:
        time_tags = {extract_time_tag(f) for f in logs}
        fw_tags = {extract_fw_tag(f) for f in logs}
        time_tag = extract_time_tag(logs[0]) if len(time_tags) == 1 else "MULTI"
        fw_tag = extract_fw_tag(logs[0]) if len(fw_tags) == 1 else "MULTI"
    else:
        time_tag, fw_tag = "notime", "nofw"

    return f"{now.strftime('%m%d%H%M')}_{time_tag}_{fw_tag}_".replace("_nofw_", "_")


# ======================================================
#                   Input selection
# ======================================================
CONTEXT_DIR_NAME = "context"

SITE_SURVEY = "site-survey"
SSID_CAPABILITY = "ssid-capability"
WIFI_CLIENTS = "wifi-clients"
KINDS: tuple[str, ...] = (SITE_SURVEY, SSID_CAPABILITY, WIFI_CLIENTS)


def _snapshot_re(kind: str) -> re.Pattern[str]:
    """``<kind>-<dut>-<YYYYmmdd-HHMMSS>.json``.

    The fixed-width timestamp anchors the parse for hyphenated DUT ids, and the
    strict ``.json`` tail is what keeps ``capture-report.txt`` and any stray
    ``.skip.json`` marker out of the input set.
    """
    return re.compile(rf"^{re.escape(kind)}-(?P<dut>.+)-(?P<ts>\d{{8}}-\d{{6}})\.json$")


def latest_snapshot(context_dir: Path, kind: str) -> Path | None:
    """Newest snapshot JSON for one kind, or None when there is none.

    Timestamps are zero-padded fixed width, so plain string ordering is
    chronological. A missing directory is a normal outcome (that kind was never
    captured), not an error.
    """
    directory = context_dir / kind
    if not directory.is_dir():
        return None
    pattern = _snapshot_re(kind)
    candidates: list[tuple[str, str, Path]] = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        m = pattern.match(path.name)
        if m:
            candidates.append((m["ts"], path.name, path))
    if not candidates:
        return None
    return max(candidates, key=lambda c: (c[0], c[1]))[2]


def load_snapshot(path: Path) -> dict:
    """Parse a snapshot JSON; an unreadable or non-object file reads as empty.

    A corrupt snapshot must degrade to "nothing to render" (§7), never crash a
    download that is otherwise fine.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[WARN] {path.name}: unreadable ({exc}) — treated as empty")
        return {}
    return data if isinstance(data, dict) else {}


# ======================================================
#              Data prep (pure, no matplotlib)
# ======================================================
# Band label -> output filename slug. The three labels are the freq-derived
# spellings used by wifi_survey / site_survey; anything else is not a band this
# tool knows how to name a file for.
BAND_SLUG: dict[str, str] = {"2.4GHz": "2g4", "5GHz": "5g", "6GHz": "6g"}

# 2.4 GHz is drawn on the full grid whether or not a channel was observed, so
# the empty channels are visible as the gaps they are (same as the UI).
ALL_24G_CHANNELS: tuple[int, ...] = tuple(range(1, 14))

ROLE_RECOMMENDED = "recommended"
ROLE_BUSIEST = "busiest"
ROLE_PLAIN = "plain"

MISSING = "—"


def _as_int(value: object) -> int | None:
    """Channel-ish value as int, or None. JSON object keys arrive as strings."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def channel_counts(neighbors: list[dict], band: str) -> dict[int, int]:
    """How many neighbouring BSS were seen on each channel of one band."""
    counts: dict[int, int] = {}
    for neighbor in neighbors:
        if neighbor.get("band") != band:
            continue
        channel = _as_int(neighbor.get("channel"))
        if channel is None:
            continue
        counts[channel] = counts.get(channel, 0) + 1
    return counts


def prepare_survey_bands(payload: dict) -> list[dict]:
    """One chart-ready dict per band the DUT has a recommendation for.

    Each dict is::

        {band, slug, iface, channels, counts, current_channel,
         recommended_channel, busiest_channel, occupancy, score, reasoning}

    ``counts`` is aligned with ``channels`` and holds raw neighbour counts (see
    the module docstring on why this is not ``occupancy``). The current and
    recommended channels are always in ``channels`` even when nothing was
    observed on them — an empty channel is precisely what gets recommended, and
    a chart that hid it would hide the answer.

    ``busiest_channel`` is the lowest-numbered channel holding the peak count,
    or None when nothing at all was observed in the band.
    """
    recommendations = payload.get("recommendations") or []
    neighbors = payload.get("neighbors") or []
    if not isinstance(recommendations, list) or not isinstance(neighbors, list):
        return []

    charts: list[dict] = []
    for rec in recommendations:
        if not isinstance(rec, dict):
            continue
        band = rec.get("band")
        slug = BAND_SLUG.get(band or "")
        if slug is None:
            print(f"[WARN] site-survey: unknown band {band!r} — no chart for it")
            continue

        current = _as_int(rec.get("current_channel"))
        recommended = _as_int(rec.get("recommended_channel"))
        counts = channel_counts(neighbors, band)

        pinned = {c for c in (current, recommended) if c is not None}
        if band == "2.4GHz":
            channels = sorted(set(ALL_24G_CHANNELS) | pinned)
        else:
            channels = sorted(set(counts) | pinned)
        values = [counts.get(channel, 0) for channel in channels]

        peak = max(values, default=0)
        busiest = next((c for c, v in zip(channels, values) if v == peak), None) if peak > 0 else None

        raw_occupancy = rec.get("occupancy") or {}
        occupancy: dict[int, float] = {}
        if isinstance(raw_occupancy, dict):
            for key, value in raw_occupancy.items():
                channel = _as_int(key)
                if channel is not None:
                    try:
                        occupancy[channel] = float(value)
                    except (TypeError, ValueError):
                        continue

        charts.append(
            {
                "band": band,
                "slug": slug,
                "iface": rec.get("iface"),
                "channels": channels,
                "counts": values,
                "current_channel": current,
                "recommended_channel": recommended,
                "busiest_channel": busiest,
                "occupancy": occupancy,
                "score": rec.get("score"),
                "reasoning": rec.get("reasoning") or "",
            }
        )
    return charts


def bar_roles(chart: dict) -> list[str]:
    """Per-bar role, aligned with ``chart["channels"]``.

    Recommended wins over busiest when they are the same channel — the useful
    statement is "stay here", not "this is crowded". The current channel is not
    a role: it is marked on the axis label, so it can coexist with either.
    """
    roles: list[str] = []
    for channel in chart["channels"]:
        if channel == chart["recommended_channel"]:
            roles.append(ROLE_RECOMMENDED)
        elif channel == chart["busiest_channel"]:
            roles.append(ROLE_BUSIEST)
        else:
            roles.append(ROLE_PLAIN)
    return roles


def _cell(value: object) -> str:
    """One table cell. Absent stays visibly absent, never a plausible zero."""
    if value is None or value == "" or value == []:
        return MISSING
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return str(value)


CAPABILITY_COLUMNS: tuple[str, ...] = ("iface", "SSID", "band", "channel", "security", "PMF", "gen")
_CAPABILITY_FIELDS: tuple[str, ...] = ("iface", "ssid", "band", "channel", "security", "pmf", "generation")

CLIENT_COLUMNS: tuple[str, ...] = (
    "MAC",
    "SSID",
    "band",
    "ch",
    "RSSI",
    "SNR",
    "tx/rx rate",
    "connected",
    "vendor",
)


def prepare_capability_table(payload: dict) -> dict:
    """``{columns, rows}`` for the DUT's own VAP configuration."""
    ssids = payload.get("ssids") or []
    if not isinstance(ssids, list):
        return {"columns": list(CAPABILITY_COLUMNS), "rows": []}
    rows = [
        [_cell(entry.get(field)) for field in _CAPABILITY_FIELDS]
        for entry in ssids
        if isinstance(entry, dict)
    ]
    return {"columns": list(CAPABILITY_COLUMNS), "rows": rows}


def prepare_clients_table(payload: dict) -> dict:
    """``{columns, rows}`` for the association tables.

    Tx and Rx rate share one column: they are the same measurement in two
    directions and reading them side by side is how the UI presents them.
    """
    clients = payload.get("clients") or []
    if not isinstance(clients, list):
        return {"columns": list(CLIENT_COLUMNS), "rows": []}
    rows: list[list[str]] = []
    for client in clients:
        if not isinstance(client, dict):
            continue
        rows.append(
            [
                _cell(client.get("mac")),
                _cell(client.get("ssid")),
                _cell(client.get("band")),
                _cell(client.get("channel")),
                _cell(client.get("rssi")),
                _cell(client.get("snr")),
                f"{_cell(client.get('txrate'))} / {_cell(client.get('rxrate'))}",
                _cell(client.get("assoc_time")),
                _cell(client.get("vendor")),
            ]
        )
    return {"columns": list(CLIENT_COLUMNS), "rows": rows}


# ======================================================
#                  Render (matplotlib)
# ======================================================
_ROLE_COLORS = {
    ROLE_RECOMMENDED: "#2e9e5b",  # green — "Best" in the UI
    ROLE_BUSIEST: "#d64545",  # red   — "Busy" in the UI
    ROLE_PLAIN: "#7f8c9b",
}
_ROLE_TAGS = {ROLE_RECOMMENDED: "Best", ROLE_BUSIEST: "Busy"}
_HEADER_BG = "#dfe4ea"
_STRIPE_BG = "#f4f6f8"


def _caption(prefix: str, source_name: str, generated_at: str) -> str:
    return (
        f"Generated_At: {generated_at} | Version: {VERSION_NAME} | "
        f"Prefix: {prefix} | Source: {source_name}"
    )


def render_band_chart(chart: dict, out_path: Path, caption: str) -> None:
    """One band's channel-usage bar chart (analyzer3 figure conventions)."""
    channels: list[int] = chart["channels"]
    counts: list[int] = chart["counts"]
    roles = bar_roles(chart)
    colors = [_ROLE_COLORS[role] for role in roles]

    # The figure stays 14x6 like every analyzer3 plot (the footer caption is
    # sized for it). A band with only two candidate channels would otherwise
    # draw two 3-inch slabs, so pad the axis to a minimum slot count and centre
    # the bars in it instead of stretching them.
    slots = max(len(channels), 8)
    left_pad = (slots - len(channels)) / 2
    positions = [left_pad + i for i in range(len(channels))]
    bar_width = 0.8 if len(channels) >= 6 else 0.6

    fig = plt.figure(figsize=(14, 6))
    ax = fig.gca()
    bars = ax.bar(positions, counts, width=bar_width, color=colors, edgecolor="#33383d", linewidth=0.6)
    ax.set_xlim(-0.5, slots - 0.5)

    top = max(4, max(counts, default=0) + 1)
    ax.set_ylim(0, top)
    for rect, value, role in zip(bars, counts, roles):
        x = rect.get_x() + rect.get_width() / 2
        highlighted = role != ROLE_PLAIN
        ax.text(
            x,
            value + top * 0.01,
            str(value),
            ha="center",
            va="bottom",
            fontsize=9,
            color=_ROLE_COLORS[role] if highlighted else "#33383d",
            fontweight="bold" if highlighted else "normal",
        )
        # The recommended channel is routinely the one with nothing on it, so a
        # zero-height bar still has to be findable — the UI tags it the same way.
        tag = _ROLE_TAGS.get(role)
        if tag:
            ax.text(
                x,
                value + top * 0.06,
                tag,
                ha="center",
                va="bottom",
                fontsize=9,
                color=_ROLE_COLORS[role],
                fontweight="bold",
            )

    current = chart["current_channel"]
    # "6 •" mirrors the dot the UI puts on the current channel's axis label.
    ax.set_xticks(positions)
    ax.set_xticklabels([f"{ch} •" if ch == current else str(ch) for ch in channels])
    ax.set_xlabel("channel")
    ax.set_ylabel("neighbouring SSIDs observed")
    ax.grid(True, axis="y", alpha=0.3)

    iface = chart.get("iface") or "?"
    ax.set_title(f"{chart['band']} channel usage (SSIDs per channel) — {iface}")

    handles = [
        Patch(facecolor=_ROLE_COLORS[ROLE_RECOMMENDED], label=f"recommended (ch {chart['recommended_channel']})"),
    ]
    if chart["busiest_channel"] is not None and chart["busiest_channel"] != chart["recommended_channel"]:
        handles.append(Patch(facecolor=_ROLE_COLORS[ROLE_BUSIEST], label=f"busiest (ch {chart['busiest_channel']})"))
    handles.append(Patch(facecolor="none", edgecolor="none", label=f"• current (ch {current})"))
    ax.legend(handles=handles, loc="upper right", fontsize=9)

    # The signal-weighted score is a different number from the bars; say so
    # rather than letting a reader assume the bars are the score.
    score = chart.get("score")
    note = chart["reasoning"] or ""
    if score is not None:
        note = f"{note}  [interference score {score}; bars are raw SSID counts]".strip()
    if note:
        fig.text(0.01, 0.955, note, fontsize=9, color="#33383d")

    fig.text(0.01, 0.01, caption, fontsize=9)
    fig.tight_layout(rect=(0, 0.03, 1, 0.94))
    fig.savefig(out_path)
    plt.close(fig)


def render_table(table: dict, out_path: Path, title: str, caption: str) -> None:
    """Render ``{columns, rows}`` as a PNG table."""
    columns: list[str] = table["columns"]
    rows: list[list[str]] = table["rows"]

    fig = plt.figure(figsize=(14, 1.5 + 0.30 * len(rows)))
    ax = fig.add_subplot(111)
    ax.axis("off")

    mpl_table = ax.table(cellText=rows, colLabels=columns, loc="upper center", cellLoc="left")
    mpl_table.auto_set_font_size(False)
    mpl_table.set_fontsize(9)
    mpl_table.scale(1, 1.4)
    mpl_table.auto_set_column_width(col=list(range(len(columns))))

    for (row_index, _col), cell in mpl_table.get_celld().items():
        cell.set_edgecolor("#c7ced6")
        if row_index == 0:
            cell.set_facecolor(_HEADER_BG)
            cell.set_text_props(weight="bold")
        elif row_index % 2 == 0:
            cell.set_facecolor(_STRIPE_BG)

    ax.set_title(title, fontsize=12, pad=12)
    fig.text(0.01, 0.01, caption, fontsize=8)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(out_path)
    plt.close(fig)


# ======================================================
#                      Orchestration
# ======================================================
def _render_survey(context_dir: Path, session_dir: Path, prefix: str, generated_at: str) -> list[Path]:
    path = latest_snapshot(context_dir, SITE_SURVEY)
    if path is None:
        print(f"[SKIP] {SITE_SURVEY}: no snapshot JSON — nothing rendered")
        return []
    charts = prepare_survey_bands(load_snapshot(path))
    if not charts:
        print(f"[SKIP] {SITE_SURVEY}: {path.name} carries no band recommendation — nothing rendered")
        return []
    written: list[Path] = []
    caption = _caption(prefix, path.name, generated_at)
    for chart in charts:
        out_path = session_dir / f"{prefix}survey_channels_{chart['slug']}.png"
        render_band_chart(chart, out_path, caption)
        written.append(out_path)
        print(f"[OK] {out_path.name} ({chart['band']}, {len(chart['channels'])} channels)")
    return written


def _render_kind_table(
    context_dir: Path,
    session_dir: Path,
    prefix: str,
    generated_at: str,
    kind: str,
    prepare,
    filename: str,
    title: str,
) -> list[Path]:
    path = latest_snapshot(context_dir, kind)
    if path is None:
        print(f"[SKIP] {kind}: no snapshot JSON — nothing rendered")
        return []
    table = prepare(load_snapshot(path))
    if not table["rows"]:
        print(f"[SKIP] {kind}: {path.name} has no rows — nothing rendered")
        return []
    out_path = session_dir / f"{prefix}{filename}"
    render_table(table, out_path, f"{title} — {path.name}", _caption(prefix, path.name, generated_at))
    print(f"[OK] {out_path.name} ({len(table['rows'])} rows)")
    return [out_path]


def render_session(session_dir: Path, now: datetime | None = None) -> list[Path]:
    """Render every renderable context snapshot under ``session_dir``.

    Returns the PNG paths written — possibly none, which is a valid outcome and
    never an error (§7).
    """
    now = now or datetime.now()
    generated_at = now.strftime("%Y-%m-%d %H:%M:%S")
    context_dir = session_dir / CONTEXT_DIR_NAME
    if not context_dir.is_dir():
        print(f"[SKIP] no {CONTEXT_DIR_NAME}/ directory in {session_dir} — nothing to render")
        return []

    prefix = output_prefix(session_dir, now)
    print(f"[INFO] Output Prefix = {prefix}")

    written: list[Path] = []
    written += _render_survey(context_dir, session_dir, prefix, generated_at)
    written += _render_kind_table(
        context_dir,
        session_dir,
        prefix,
        generated_at,
        SSID_CAPABILITY,
        prepare_capability_table,
        "ssid_capability.png",
        "SSID capability",
    )
    written += _render_kind_table(
        context_dir,
        session_dir,
        prefix,
        generated_at,
        WIFI_CLIENTS,
        prepare_clients_table,
        "wifi_clients_table.png",
        "Associated Wi-Fi clients",
    )

    if not written:
        print("[INFO] no PNG written — every context kind was absent or empty")
    else:
        print(f"[INFO] {len(written)} PNG(s) written to {session_dir}")
    return written


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    session_dir = Path(args[0]) if args else Path(".")
    render_session(session_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
