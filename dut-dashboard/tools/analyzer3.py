import os
import re
import csv
from datetime import datetime
import matplotlib.pyplot as plt

# ======================================================
#                 Version / Metadata
# ======================================================
VERSION_NAME = "system_monitor_analyzer v1.1.2"
GENERATED_AT = datetime.now()
GENERATED_AT_STR = GENERATED_AT.strftime("%Y-%m-%d %H:%M:%S")
RUN_PREFIX_TS = GENERATED_AT.strftime("%m%d%H%M")  # mmddHHMM, i.e, 02251331

# ======================================================
#                   Auto File Detection
# ======================================================
LOG_EXT = (".log", ".txt")
log_files = sorted([f for f in os.listdir(".") if f.endswith(LOG_EXT)])

if not log_files:
    print("[ERROR] Found no log files(.log/.txt)")
    exit(1)

print(f"[INFO] Detected {len(log_files)} log files:")
for lf in log_files:
    print(" -", lf)

# ======================================================
#        Robust tag extraction from filename
#   Prefix: <mmddHHMM>_<HHMMSS>_<FW>_
# ======================================================
def extract_time_tag(filename: str) -> str:
    m = re.search(r"_(\d{6})(?:_|\.|$)", filename)
    return m.group(1) if m else "notime"

def fw_triplet_to_tag(maj: str, minor: str, patch: str) -> str:
    try:
        p = int(patch)
        return f"{int(maj)}{int(minor)}{p:03d}"  # 1.9.300 -> 19300, 1.8.241 -> 18241
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

time_tag0 = extract_time_tag(log_files[0])
fw_tag0 = extract_fw_tag(log_files[0])

if len(log_files) > 1:
    time_tags = {extract_time_tag(f) for f in log_files}
    fw_tags = {extract_fw_tag(f) for f in log_files}
    time_tag = time_tag0 if len(time_tags) == 1 else "MULTI"
    fw_tag = fw_tag0 if len(fw_tags) == 1 else "MULTI"
else:
    time_tag = time_tag0
    fw_tag = fw_tag0

OUT_PREFIX = f"{RUN_PREFIX_TS}_{time_tag}_{fw_tag}_"
OUT_PREFIX = OUT_PREFIX.replace("_nofw_", "_")   #  Removed "nofw" from filenames
print(f"[INFO] Output Prefix = {OUT_PREFIX}")

# ======================================================
#                     Output Files (with prefix)
# ======================================================
CPU_CSV   = f"{OUT_PREFIX}cpu_usage.csv"
MEM_CSV   = f"{OUT_PREFIX}memory.csv"
CPU_PLOT  = f"{OUT_PREFIX}cpu_usage_plot.png"
MEM_PLOT  = f"{OUT_PREFIX}memory_plot.png"
SPIKE_REPORT = f"{OUT_PREFIX}cpu_spike_report.txt"
MEM_AVAIL_PLOT   = f"{OUT_PREFIX}memavailable_plot.png"
SLAB_PLOT        = f"{OUT_PREFIX}slab_plot.png"
SUNRECLAIM_PLOT  = f"{OUT_PREFIX}sunreclaim_plot.png"

# ======================================================
#                     Regex Patterns
# ======================================================
ts_pattern = re.compile(r"= Test Time:\s*(\d+),\s*([\d\-]+\s+[\d:]+)")
cpu_pattern = re.compile(
    r"CPU(\d+):\s+([\d\.]+)% usr\s+([\d\.]+)% sys\s+([\d\.]+)% nic\s+([\d\.]+)% idle\s+([\d\.]+)% io\s+([\d\.]+)% irq\s+([\d\.]+)%% sirq"
)
# allow leading spaces
mem_pattern = re.compile(r"(MemAvailable|Slab|SUnreclaim):\s+(\d+)\s*kB")

# Storage
records = []
current = {}
mem_hits = 0

def flush_record():
    # Only accept real snapshot(At least with Timestamp)
    if current and "Timestamp" in current:
        records.append(current.copy())

# ======================================================
#            Step 1: Parse All Log Files
# ======================================================
for LOG_FILE in log_files:
    print(f"[INFO] Analyzing: {LOG_FILE}")
    with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\n")

            ts_match = ts_pattern.search(line)
            if ts_match:
                flush_record()
                current = {}
                current["Test_Count"] = ts_match.group(1)
                current["Timestamp"] = ts_match.group(2)
                continue

            cpu_match = cpu_pattern.search(line.strip())
            if cpu_match:
                idx = cpu_match.group(1)
                current[f"CPU{idx}_usr"] = cpu_match.group(2)
                current[f"CPU{idx}_sys"] = cpu_match.group(3)
                current[f"CPU{idx}_nic"] = cpu_match.group(4)
                current[f"CPU{idx}_idle"] = cpu_match.group(5)
                current[f"CPU{idx}_io"] = cpu_match.group(6)
                current[f"CPU{idx}_irq"] = cpu_match.group(7)
                current[f"CPU{idx}_sirq"] = cpu_match.group(8)
                continue

            mem_match = mem_pattern.search(line)
            if mem_match:
                key = mem_match.group(1)
                val = mem_match.group(2)
                current[key] = val
                mem_hits += 1  # ✅ counts
                #print(f"[MEM] {key} = {val}")
                continue

flush_record()

if not records:
    print("[ERROR] Non snapshot is analyzed(records=0),please check if the format of the log matches regex.")
    exit(1)

print(f"[OK] Analyzed {len(records)} snapshot.")
print(f"[DEBUG] mem_hits = {mem_hits}")  # Leave it here

first_mem = next((r for r in records if "Slab" in r or "MemAvailable" in r or "SUnreclaim" in r), None)
print("[DEBUG] first_mem fields:",
      None if not first_mem else first_mem.get("MemAvailable"),
      None if not first_mem else first_mem.get("Slab"),
      None if not first_mem else first_mem.get("SUnreclaim"))


# ======================================================
#            Helpers
# ======================================================
def normalize_ts(ts: str) -> str:
    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    return dt.strftime("%m%d_%H%M%S")

def to_int(v) -> int:
    try:
        return int(v)
    except Exception:
        return 0

def to_float(v) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0

bad = [i for i, r in enumerate(records) if "Timestamp" not in r]
print(f"[DEBUG] records missing Timestamp: {len(bad)}")
if bad:
    print("[DEBUG] first few bad indices:", bad[:5])
    print("[DEBUG] sample bad record:", records[bad[0]])

def safe_normalize_ts(ts: str) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%m%d_%H%M%S")
    except Exception:
        return ""

timestamps = [safe_normalize_ts(r.get("Timestamp", "")) for r in records]
x = list(range(len(timestamps)))

def thin_xticks(ax, labels, max_ticks=25):
    n = len(labels)
    if n <= max_ticks:
        ax.set_xticks(range(n))
        ax.set_xticklabels(labels, rotation=45)
        return
    step = max(1, n // max_ticks)
    idx = list(range(0, n, step))
    ax.set_xticks(idx)
    ax.set_xticklabels([labels[i] for i in idx], rotation=45)

"""def plot_series_single(x, xlabels, y, ylabel, title, out_path):
    fig = plt.figure(figsize=(14, 6))
    ax = fig.gca()

    ax.plot(x, y, label=ylabel)  # Auto colour-selection
    thin_xticks(ax, xlabels, max_ticks=25)

    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True)
    ax.legend()

    fig.text(
        0.01, 0.01,
        f"Generated_At: {GENERATED_AT_STR} | Version: {VERSION_NAME} | Prefix: {OUT_PREFIX}",
        fontsize=9
    )

    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    print(f"[OK] Rendered {out_path}")
"""
# ======================================================
#            Step 2: Dynamic CPU Utilization
# ======================================================
cpu_idx_set = set()
cpu_usr_key_re = re.compile(r"^CPU(\d+)_usr$")

for r in records:
    for k in r.keys():
        m = cpu_usr_key_re.match(k)
        if m:
            cpu_idx_set.add(int(m.group(1)))

cpu_indices = sorted(cpu_idx_set)

def cpu_total(rec, idx: int) -> float:
    usr = to_float(rec.get(f"CPU{idx}_usr", 0))
    sys = to_float(rec.get(f"CPU{idx}_sys", 0))
    irq = to_float(rec.get(f"CPU{idx}_irq", 0))
    sirq = to_float(rec.get(f"CPU{idx}_sirq", 0))
    return usr + sys + irq + sirq

SPIKE_THRESHOLD = 50.0
cpu_spikes = []
cpu_usage = {idx: [] for idx in cpu_indices}

for r in records:
    for idx in cpu_indices:
        val = cpu_total(r, idx)
        cpu_usage[idx].append(val)
        if val > SPIKE_THRESHOLD:
            cpu_spikes.append((r["Timestamp"], f"CPU{idx}", val))

# ======================================================
#            Step 3: Write cpu_usage.csv
# ======================================================
cpu_rows = []
for i, rec in enumerate(records):
    row = {
        "Timestamp": rec.get("Timestamp", ""),
        "Timestamp_MMDD_HHMMSS": timestamps[i],
        "Generated_At": GENERATED_AT_STR,
        "Version": VERSION_NAME,
        "Output_Prefix": OUT_PREFIX,
    }
    for idx in cpu_indices:
        row[f"CPU{idx}_UsagePct"] = f"{cpu_usage[idx][i]:.2f}" if cpu_indices else ""
    cpu_rows.append(row)

cpu_fieldnames = ["Timestamp", "Timestamp_MMDD_HHMMSS"] + \
                 [f"CPU{idx}_UsagePct" for idx in cpu_indices] + \
                 ["Generated_At", "Version", "Output_Prefix"]

with open(CPU_CSV, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=cpu_fieldnames)
    w.writeheader()
    w.writerows(cpu_rows)

print(f"[OK] Rendered {CPU_CSV}")

# ======================================================
#            Step 4: Write memory.csv  (kB units)
# ======================================================
mem_avail_kb = [to_int(r.get("MemAvailable", 0)) for r in records]
slab_kb      = [to_int(r.get("Slab", 0)) for r in records]
sunreclaim_kb= [to_int(r.get("SUnreclaim", 0)) for r in records]

def calc_slope(y):
    if len(y) < 2:
        return 0
    return (y[-1] - y[0]) / (len(y)-1)

print("[LEAK] Slab slope (kB/sample):", calc_slope(slab_kb))
print("[LEAK] SUnreclaim slope (kB/sample):", calc_slope(sunreclaim_kb))
print("[LEAK] MemAvail slope (kB/sample):", calc_slope(mem_avail_kb))

def delta(y):
    return (y[-1] - y[0]) if y else 0

print("[LEAK] Slab: start/end/delta(kB) =", slab_kb[0], slab_kb[-1], delta(slab_kb),
      "| slope(kB/sample) =", calc_slope(slab_kb))
print("[LEAK] SUnreclaim: start/end/delta(kB) =", sunreclaim_kb[0], sunreclaim_kb[-1], delta(sunreclaim_kb),
      "| slope(kB/sample) =", calc_slope(sunreclaim_kb))
print("[LEAK] MemAvail: start/end/delta(kB) =", mem_avail_kb[0], mem_avail_kb[-1], delta(mem_avail_kb),
      "| slope(kB/sample) =", calc_slope(mem_avail_kb))

# Key leak index（There was a author hand-drawing picture.）
#effective_kb = [max(ma - su, 0) for ma, su in zip(mem_avail_kb, sunreclaim_kb)]
effective_kb = [ma - su for ma, su in zip(mem_avail_kb, sunreclaim_kb)]
mem_rows = []
for i, rec in enumerate(records):
    mem_rows.append({
        "Timestamp": rec.get("Timestamp", ""),
        "Timestamp_MMDD_HHMMSS": timestamps[i],
        "MemAvailable_kB": mem_avail_kb[i],
        "Slab_kB": slab_kb[i],
        "SUnreclaim_kB": sunreclaim_kb[i],
        "EffectiveAvailable_kB": effective_kb[i],
        "Generated_At": GENERATED_AT_STR,
        "Version": VERSION_NAME,
        "Output_Prefix": OUT_PREFIX,
    })

mem_fieldnames = [
    "Timestamp", "Timestamp_MMDD_HHMMSS",
    "MemAvailable_kB", "Slab_kB", "SUnreclaim_kB", "EffectiveAvailable_kB",
    "Generated_At", "Version", "Output_Prefix"
]

with open(MEM_CSV, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=mem_fieldnames)
    w.writeheader()
    w.writerows(mem_rows)

print(f"[OK] Rendered {MEM_CSV}")

# ======================================================
#            Step 5: CPU Plot
# ======================================================
if cpu_indices:
    fig = plt.figure(figsize=(14, 6))
    ax = fig.gca()
    for idx in cpu_indices:
        ax.plot(x, cpu_usage[idx], label=f"CPU{idx} Usage (%)")
    thin_xticks(ax, timestamps, max_ticks=25)
    ax.set_ylabel("CPU Usage (%)")
    ax.set_title(f"CPU Usage Over Time | {VERSION_NAME}")
    ax.grid(True)
    ax.legend(ncol=2)
    fig.text(0.01, 0.01,
             f"Generated_At: {GENERATED_AT_STR} | Version: {VERSION_NAME} | Prefix: {OUT_PREFIX}",
             fontsize=9)
    fig.tight_layout()
    fig.savefig(CPU_PLOT)
    plt.close(fig)
    print(f"[OK] Rendered {CPU_PLOT}")
else:
    print("[WARN] Found no records related to 'CPU*_usr' column, skip CPU plot.")

def plot_series_single(x, xlabels, y, ylabel, title, out_path):
    fig = plt.figure(figsize=(14, 6))
    ax = fig.gca()

    ax.plot(x, y, label=ylabel)  # Auto colour-selection
    thin_xticks(ax, xlabels, max_ticks=25)

    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True)
    ax.legend()

    fig.text(
        0.01, 0.01,
        f"Generated_At: {GENERATED_AT_STR} | Version: {VERSION_NAME} | Prefix: {OUT_PREFIX}",
        fontsize=9
    )

    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    print(f"[OK] Rendered {out_path}")

# ======================================================
#            Step 6: Memory Plots (separate PNGs, kB)
# ======================================================
plot_series_single(
    x, timestamps, mem_avail_kb,
    "MemAvailable (kB)",
    f"MemAvailable Over Time | {VERSION_NAME}",
    MEM_AVAIL_PLOT
)

plot_series_single(
    x, timestamps, slab_kb,
    "Slab (kB)",
    f"Slab Over Time | {VERSION_NAME}",
    SLAB_PLOT
)

plot_series_single(
    x, timestamps, sunreclaim_kb,
    "SUnreclaim (kB)",
    f"SUnreclaim Over Time | {VERSION_NAME}",
    SUNRECLAIM_PLOT
)

# ======================================================
#          Step 7: CPU Spike Report >50%
# ======================================================
with open(SPIKE_REPORT, "w", encoding="utf-8") as f:
    f.write("CPU Spike Report (>50%)\n")
    f.write("=========================\n")
    f.write(f"Generated_At: {GENERATED_AT_STR}\n")
    f.write(f"Version     : {VERSION_NAME}\n")
    f.write(f"Prefix      : {OUT_PREFIX}\n")
    f.write(f"Log_Files   : {', '.join(log_files)}\n\n")

    if not cpu_spikes:
        f.write("[INFO] No spikes detected.\n")
    else:
        for ts, cpu, val in cpu_spikes:
            f.write(f"{ts}  {cpu}: {val:.2f}%\n")

print(f"[OK] Rendered {SPIKE_REPORT}")
print("\n=== Done, all rendering is fulfilled. ===") 