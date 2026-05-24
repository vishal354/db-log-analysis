"""
MySQL DB-Master Log Analysis Script
====================================
Parses the sanitised process_list log CSV and generates
a visual report with charts saved as PNG files.

Usage:
    pip install pandas matplotlib
    python analyze_logs.py

Output:
    - report_figures/ folder with PNG charts
    - Console summary of findings
"""

import csv
import re
import os
from collections import defaultdict

try:
    import pandas as pd
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    HAS_PLOTTING = True
except ImportError:
    HAS_PLOTTING = False
    print("Install pandas and matplotlib for charts: pip install pandas matplotlib")

# ─── CONFIG ───
LOG_FILE = "logs/Logs-sanitised-2026-04-17_For Assesment.csv"
OUTPUT_DIR = "report_figures"

# ─── REGEX PATTERNS ───
RE_LOAD_AVG = re.compile(r"load average:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)")
RE_CPU = re.compile(r"Cpu\(s\):\s*([\d.]+)%us,\s*([\d.]+)%sy,.*?([\d.]+)%id,\s*([\d.]+)%wa")
RE_MEM = re.compile(r"Mem:\s+(\d+)k total,\s+(\d+)k used,\s+(\d+)k free")
RE_SWAP = re.compile(r"Swap:\s+(\d+)k total,\s+(\d+)k used")
RE_TRX_COUNTER = re.compile(r"Trx id counter\s+(\d+)")
RE_PURGE = re.compile(r"Purge done for trx's n:o < (\d+)")
RE_HISTORY = re.compile(r"History list length\s+(\d+)")
RE_QUERY_COUNT = re.compile(r"IP:\s+([\d.]+)\s+No\. of Queries:\s+(\d+)")
RE_BUFFER_HIT = re.compile(r"Buffer pool hit rate\s+(\d+)\s*/\s*(\d+)")
RE_IO_RATES = re.compile(r"([\d.]+)\s+reads/s,\s*([\d.]+)\s+creates/s,\s*([\d.]+)\s+writes/s")
RE_LOG_FLUSHED = re.compile(r"Log flushed up to\s+(\d+)")
RE_TRACE_FAIL = re.compile(r"Trace failed|Skipping trace|Network error|process no longer exists|ERROR:")
RE_MYSQLD_PROC = re.compile(r"mysql\s+(\d+)\s+\d+\s+(\d+)\s+(\S+)")
RE_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}\s+(\d{2}:\d{2}:\d{2})")


def parse_log(filepath):
    """Parse the CSV log and extract all metrics per time snapshot."""

    load_avg = []          # (time, 1m, 5m, 15m)
    cpu_data = []          # (time, us, sy, id, wa)
    mem_data = []          # (time, total_gb, used_gb, free_gb)
    swap_data = []         # (time, used_mb)
    trx_counters = []      # (time, counter)
    purge_values = []      # (time, purge_lsn)
    history_lengths = []   # (time, length)
    query_counts = defaultdict(list)   # {time: [(ip, count), ...]}
    buffer_hits = []       # (time, [rates...])
    io_reads = []          # (time, total_reads)
    io_writes = []         # (time, total_writes)
    log_lsn = []           # (time, lsn)
    error_count = 0
    error_types = defaultdict(int)
    mysqld_procs = {}      # {pid: cpu%}

    current_time = None
    current_buf_hits = []
    current_reads = 0.0
    current_writes = 0.0

    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)

        for row in reader:
            if len(row) < 3:
                continue
            line_content = row[2] if len(row) > 2 else ""

            # Extract timestamp from the log line
            ts_match = RE_TIMESTAMP.search(line_content)

            # Load average
            m = RE_LOAD_AVG.search(line_content)
            if m:
                t = ts_match.group(1) if ts_match else current_time
                current_time = t
                load_avg.append((t, float(m.group(1)), float(m.group(2)), float(m.group(3))))

            # CPU
            m = RE_CPU.search(line_content)
            if m:
                t = ts_match.group(1) if ts_match else current_time
                cpu_data.append((t, float(m.group(1)), float(m.group(2)),
                                 float(m.group(3)), float(m.group(4))))

            # Memory
            m = RE_MEM.search(line_content)
            if m:
                t = ts_match.group(1) if ts_match else current_time
                total_gb = int(m.group(1)) / 1048576
                used_gb = int(m.group(2)) / 1048576
                free_gb = int(m.group(3)) / 1048576
                mem_data.append((t, total_gb, used_gb, free_gb))

            # Swap
            m = RE_SWAP.search(line_content)
            if m:
                t = ts_match.group(1) if ts_match else current_time
                swap_data.append((t, int(m.group(2)) / 1024))

            # Transaction counter
            m = RE_TRX_COUNTER.search(line_content)
            if m:
                t = ts_match.group(1) if ts_match else current_time
                trx_counters.append((t, int(m.group(1))))

            # Purge status
            m = RE_PURGE.search(line_content)
            if m:
                t = ts_match.group(1) if ts_match else current_time
                purge_values.append((t, int(m.group(1))))

            # History list
            m = RE_HISTORY.search(line_content)
            if m:
                t = ts_match.group(1) if ts_match else current_time
                history_lengths.append((t, int(m.group(1))))

            # Query count by IP
            m = RE_QUERY_COUNT.search(line_content)
            if m:
                t = ts_match.group(1) if ts_match else current_time
                ip = m.group(1)
                count = int(m.group(2))
                query_counts[t].append((ip, count))

            # Buffer pool hit rate
            m = RE_BUFFER_HIT.search(line_content)
            if m:
                current_buf_hits.append(int(m.group(1)))

            # I/O rates
            m = RE_IO_RATES.search(line_content)
            if m:
                current_reads += float(m.group(1))
                current_writes += float(m.group(3))

            # Log flushed LSN
            m = RE_LOG_FLUSHED.search(line_content)
            if m:
                t = ts_match.group(1) if ts_match else current_time
                lsn = int(m.group(1))
                log_lsn.append((t, lsn))

                # At the end of each InnoDB status block, save accumulated I/O
                if current_reads > 0 or current_writes > 0:
                    io_reads.append((t, current_reads))
                    io_writes.append((t, current_writes))
                    current_reads = 0.0
                    current_writes = 0.0
                if current_buf_hits:
                    buffer_hits.append((t, list(current_buf_hits)))
                    current_buf_hits = []

            # Errors
            if RE_TRACE_FAIL.search(line_content):
                error_count += 1
                if "Network error" in line_content:
                    error_types["Network error"] += 1
                elif "password prompt" in line_content:
                    error_types["Password prompt failure"] += 1
                elif "no longer exists" in line_content:
                    error_types["Process gone"] += 1
                elif "ERROR:" in line_content:
                    error_types["Process ID error"] += 1
                else:
                    error_types["Other"] += 1

            # mysqld processes
            m = RE_MYSQLD_PROC.search(line_content)
            if m and "mysqld" in line_content:
                pid = m.group(1)
                cpu = int(m.group(2))
                mysqld_procs[pid] = cpu

    return {
        "load_avg": load_avg,
        "cpu": cpu_data,
        "mem": mem_data,
        "swap": swap_data,
        "trx": trx_counters,
        "purge": purge_values,
        "history": history_lengths,
        "queries": query_counts,
        "buffer_hits": buffer_hits,
        "io_reads": io_reads,
        "io_writes": io_writes,
        "log_lsn": log_lsn,
        "errors": error_count,
        "error_types": dict(error_types),
        "mysqld_procs": dict(mysqld_procs),
    }


def print_summary(data):
    """Print a text summary of findings."""

    print("\n" + "=" * 60)
    print("  DATABASE SERVER HEALTH REPORT")
    print("  Host: svc-db-node-001.internal.example-corp.com")
    print("  Period: 2026-04-16 01:18 - 01:25 UTC")
    print("=" * 60)

    # Memory
    if data["mem"]:
        _, total, used, free = data["mem"][0]
        pct = (used / total) * 100
        print(f"\n[MEMORY]  {used:.1f} GB used / {total:.1f} GB total ({pct:.1f}%) — Free: {free:.2f} GB")
        if pct > 95:
            print("  [!] CRITICAL: Less than 5% memory free")

    # Load average trend
    if data["load_avg"]:
        first = data["load_avg"][0]
        last = data["load_avg"][-1]
        print(f"\n[LOAD]    1-min: {first[1]} -> {last[1]}  |  5-min: {first[2]} -> {last[2]}  |  15-min: {first[3]} -> {last[3]}")

    # CPU
    if data["cpu"]:
        _, us, sy, idle, wa = data["cpu"][0]
        print(f"\n[CPU]     User: {us}%  System: {sy}%  Idle: {idle}%  IO-Wait: {wa}%")
        if wa > 2:
            print(f"  [!] IO-Wait {wa}% indicates disk pressure")

    # InnoDB purge
    if data["trx"] and data["purge"]:
        trx_now = data["trx"][-1][1]
        purge_at = data["purge"][-1][1]
        gap = trx_now - purge_at
        purge_same = len(set(p[1] for p in data["purge"])) == 1
        print(f"\n[PURGE]   Trx counter: {trx_now:,}  Purge at: {purge_at:,}  Gap: {gap:,}")
        if purge_same:
            print("  [!] CRITICAL: Purge has NOT advanced -- likely blocked by a long-running transaction")

    # History list
    if data["history"]:
        first_h = data["history"][0][1]
        last_h = data["history"][-1][1]
        print(f"\n[HISTORY] {first_h} -> {last_h} (growing +{last_h - first_h} in {len(data['history'])} snapshots)")
        if last_h > 1000:
            print(f"  [!] History list {last_h} is high (healthy < 1000)")

    # TPS calculation
    if len(data["trx"]) >= 2:
        tps_list = []
        for i in range(1, len(data["trx"])):
            delta = data["trx"][i][1] - data["trx"][i - 1][1]
            tps_list.append(delta / 60)
        avg_tps = sum(tps_list) / len(tps_list)
        print(f"\n[TPS]     Average: ~{avg_tps:.1f} transactions/sec")

    # Query count summary
    if data["queries"]:
        ip_totals = defaultdict(int)
        for t, pairs in data["queries"].items():
            for ip, count in pairs:
                ip_totals[ip] += count
        top_ips = sorted(ip_totals.items(), key=lambda x: -x[1])[:5]
        total_q = sum(ip_totals.values())
        print(f"\n[QPS]     Total queries across all snapshots: {total_q}")
        print("  Top 5 IPs:")
        for ip, cnt in top_ips:
            pct = (cnt / total_q) * 100
            print(f"    {ip:20s}  {cnt:4d} queries  ({pct:.1f}%)")

    # mysqld processes
    if data["mysqld_procs"]:
        print(f"\n[PROCS]   {len(data['mysqld_procs'])} mysqld processes detected")
        for pid, cpu in sorted(data["mysqld_procs"].items(), key=lambda x: -x[1]):
            print(f"    PID {pid}: {cpu}% CPU")
        if len(data["mysqld_procs"]) > 1:
            print("  [!] Multiple mysqld instances -- resource contention risk")

    # Errors
    if data["errors"]:
        print(f"\n[ERRORS]  {data['errors']} total errors")
        for etype, cnt in sorted(data["error_types"].items(), key=lambda x: -x[1]):
            print(f"    {etype}: {cnt}")

    print("\n" + "=" * 60)


def generate_charts(data):
    """Generate PNG charts for the report."""

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    plt.style.use("seaborn-v0_8-darkgrid")

    # ── Chart 1: Load Average ──
    if data["load_avg"]:
        fig, ax = plt.subplots(figsize=(10, 4))
        times = [x[0][:5] for x in data["load_avg"]]
        ax.plot(times, [x[1] for x in data["load_avg"]], "o-", label="1-min", linewidth=2)
        ax.plot(times, [x[2] for x in data["load_avg"]], "s-", label="5-min", linewidth=2)
        ax.plot(times, [x[3] for x in data["load_avg"]], "^-", label="15-min", linewidth=2)
        ax.set_title("System Load Average Over Time", fontsize=14, fontweight="bold")
        ax.set_xlabel("Time (UTC)")
        ax.set_ylabel("Load Average")
        ax.legend()
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(f"{OUTPUT_DIR}/01_load_average.png", dpi=150)
        plt.close(fig)
        print(f"  Saved {OUTPUT_DIR}/01_load_average.png")

    # ── Chart 2: Memory Usage ──
    if data["mem"]:
        fig, ax = plt.subplots(figsize=(10, 4))
        times = [x[0][:5] for x in data["mem"]]
        used = [x[2] for x in data["mem"]]
        free = [x[3] for x in data["mem"]]
        ax.bar(times, used, label="Used (GB)", color="#e74c3c", alpha=0.8)
        ax.bar(times, free, bottom=used, label="Free (GB)", color="#2ecc71", alpha=0.8)
        ax.set_title("Memory Usage Over Time", fontsize=14, fontweight="bold")
        ax.set_xlabel("Time (UTC)")
        ax.set_ylabel("Memory (GB)")
        ax.legend()
        ax.set_ylim(0, 16)
        fig.tight_layout()
        fig.savefig(f"{OUTPUT_DIR}/02_memory_usage.png", dpi=150)
        plt.close(fig)
        print(f"  Saved {OUTPUT_DIR}/02_memory_usage.png")

    # ── Chart 3: InnoDB History List ──
    if data["history"]:
        fig, ax = plt.subplots(figsize=(10, 4))
        times = [x[0][:5] for x in data["history"]]
        lengths = [x[1] for x in data["history"]]
        ax.fill_between(times, lengths, alpha=0.3, color="#e74c3c")
        ax.plot(times, lengths, "o-", color="#e74c3c", linewidth=2, markersize=6)
        ax.axhline(y=1000, color="orange", linestyle="--", label="Warning threshold (1000)")
        ax.set_title("InnoDB History List Length (Purge Lag)", fontsize=14, fontweight="bold")
        ax.set_xlabel("Time (UTC)")
        ax.set_ylabel("History List Length")
        ax.legend()
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(f"{OUTPUT_DIR}/03_history_list.png", dpi=150)
        plt.close(fig)
        print(f"  Saved {OUTPUT_DIR}/03_history_list.png")

    # ── Chart 4: InnoDB I/O Rates ──
    if data["io_reads"] and data["io_writes"]:
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 4))
        times_r = [x[0][:5] for x in data["io_reads"]]
        times_w = [x[0][:5] for x in data["io_writes"]]

        ax1.bar(times_r, [x[1] for x in data["io_reads"]], color="#3498db", alpha=0.8)
        ax1.set_title("InnoDB Reads/s (all pools)", fontsize=12, fontweight="bold")
        ax1.set_xlabel("Time (UTC)")
        ax1.set_ylabel("Reads/s")

        ax2.bar(times_w, [x[1] for x in data["io_writes"]], color="#e74c3c", alpha=0.8)
        ax2.set_title("InnoDB Writes/s (all pools)", fontsize=12, fontweight="bold")
        ax2.set_xlabel("Time (UTC)")
        ax2.set_ylabel("Writes/s")

        fig.suptitle("InnoDB I/O Activity", fontsize=14, fontweight="bold")
        fig.tight_layout()
        fig.savefig(f"{OUTPUT_DIR}/04_innodb_io.png", dpi=150)
        plt.close(fig)
        print(f"  Saved {OUTPUT_DIR}/04_innodb_io.png")

    # ── Chart 5: QPS by IP (top 5) ──
    if data["queries"]:
        ip_totals = defaultdict(int)
        for t, pairs in data["queries"].items():
            for ip, count in pairs:
                ip_totals[ip] += count
        top5 = sorted(ip_totals.items(), key=lambda x: -x[1])[:5]

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

        # Pie chart
        labels = [x[0] for x in top5] + ["Others"]
        values = [x[1] for x in top5] + [sum(ip_totals.values()) - sum(x[1] for x in top5)]
        colors = ["#e74c3c", "#3498db", "#2ecc71", "#f39c12", "#9b59b6", "#95a5a6"]
        ax1.pie(values, labels=labels, colors=colors, autopct="%1.0f%%", startangle=90)
        ax1.set_title("Query Distribution by Client IP", fontsize=12, fontweight="bold")

        # Time series for top IP
        sorted_times = sorted(data["queries"].keys())
        top_ip = top5[0][0]
        ip_over_time = []
        for t in sorted_times:
            total_at_t = sum(c for _, c in data["queries"][t])
            ip_over_time.append(total_at_t)

        ax2.bar(range(len(sorted_times)), ip_over_time, color="#3498db", alpha=0.8)
        ax2.set_xticks(range(len(sorted_times)))
        ax2.set_xticklabels([t[:5] for t in sorted_times], rotation=45)
        ax2.set_title("Total Queries per Snapshot", fontsize=12, fontweight="bold")
        ax2.set_xlabel("Time (UTC)")
        ax2.set_ylabel("Query Count")

        fig.tight_layout()
        fig.savefig(f"{OUTPUT_DIR}/05_qps_analysis.png", dpi=150)
        plt.close(fig)
        print(f"  Saved {OUTPUT_DIR}/05_qps_analysis.png")

    # ── Chart 6: Redo Log Growth ──
    if len(data["log_lsn"]) >= 2:
        fig, ax = plt.subplots(figsize=(10, 4))
        deltas = []
        labels = []
        for i in range(1, len(data["log_lsn"])):
            delta_kb = (data["log_lsn"][i][1] - data["log_lsn"][i - 1][1]) / 1024
            deltas.append(delta_kb)
            labels.append(f"{data['log_lsn'][i-1][0][:5]}→{data['log_lsn'][i][0][:5]}")

        colors = ["#2ecc71" if d < 200 else "#f39c12" if d < 500 else "#e74c3c" for d in deltas]
        ax.bar(labels, deltas, color=colors, alpha=0.8)
        ax.set_title("Redo Log Growth Rate (KB per interval)", fontsize=14, fontweight="bold")
        ax.set_xlabel("Time Interval")
        ax.set_ylabel("KB written")
        ax.grid(True, alpha=0.3, axis="y")
        fig.tight_layout()
        fig.savefig(f"{OUTPUT_DIR}/06_redo_log_growth.png", dpi=150)
        plt.close(fig)
        print(f"  Saved {OUTPUT_DIR}/06_redo_log_growth.png")

    # ── Chart 7: Error breakdown ──
    if data["error_types"]:
        fig, ax = plt.subplots(figsize=(8, 5))
        types = list(data["error_types"].keys())
        counts = list(data["error_types"].values())
        colors = ["#e74c3c", "#f39c12", "#3498db", "#9b59b6", "#95a5a6"]
        ax.barh(types, counts, color=colors[: len(types)], alpha=0.8)
        ax.set_title(f"Error Breakdown ({data['errors']} total)", fontsize=14, fontweight="bold")
        ax.set_xlabel("Count")
        for i, v in enumerate(counts):
            ax.text(v + 0.5, i, str(v), va="center", fontweight="bold")
        fig.tight_layout()
        fig.savefig(f"{OUTPUT_DIR}/07_errors.png", dpi=150)
        plt.close(fig)
        print(f"  Saved {OUTPUT_DIR}/07_errors.png")

    print(f"\n  All charts saved to {OUTPUT_DIR}/")


# ─── MAIN ───
if __name__ == "__main__":
    print("Parsing log file...")
    data = parse_log(LOG_FILE)

    print_summary(data)

    if HAS_PLOTTING:
        print("\nGenerating charts...")
        generate_charts(data)
    else:
        print("\nSkipping charts (install: pip install pandas matplotlib)")

    print("\nDone.")
