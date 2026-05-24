# MySQL DB-Master Log Analysis

**Host:** svc-db-node-001.internal.example-corp.com (db-master)  
**Period:** 2026-04-16 01:18--01:25 UTC (8 snapshots, ~1-min intervals)  
**Data:** 5,344 log lines from /mnt/ops_logs/process_list.log

---

## Summary

The server has **critical InnoDB purge stall** (history list 4,735 -> 4,859 and growing), **96.6% RAM used** with only 500 MB free, **4 mysqld processes** competing for resources, and **one client IP driving 43% of all queries**. An 18x I/O write spike at 01:24 indicates an unmanaged batch operation.

---

## 1. Issues Observed

### 1.1 InnoDB Purge Stalled (Critical)

Purge is frozen at trx 5,845,989,957 across all snapshots while the transaction counter advanced to 5,846,038,526 -- a gap of ~48,500 unpurged transactions. The history list grew from 4,735 to 4,859 (+124 in 7 min). Healthy is below 1,000.

![History List](report_figures/03_history_list.png)

**Root cause:** A long-running or abandoned transaction is blocking the purge thread from cleaning up old row versions.  
**Impact:** Undo tablespace bloat, slower reads (longer MVCC version chains), buffer pool pollution.

### 1.2 Severe Memory Pressure (Critical)

![Memory Usage](report_figures/02_memory_usage.png)

14.2 GB used of 14.7 GB total (96.6%). Only ~500 MB free. Swap is low (~97 MB / 8 GB) but there is zero headroom -- any spike from connections, sorts, or temp tables risks swap thrashing.

### 1.3 Four mysqld Processes (High)

| PID | CPU % | Started | Notes |
|-----|-------|---------|-------|
| 4374 | 38% | 2025 (111+ days) | Primary master |
| 1744 | **59%** | Apr 05 (~11 days) | Highest CPU -- investigate |
| 1775947 | 36% | Apr 13 (~3 days) | Recent instance |
| 29467 | 13% | Apr 15 (~1 day) | Newest |

Combined 146% CPU. Each has its own buffer pool, fragmenting cache across 4 processes instead of one large pool. Staggered start dates suggest orphaned processes from failed restarts.

### 1.4 I/O Write Spike at 01:24 (High)

![InnoDB I/O](report_figures/04_innodb_io.png)

InnoDB writes surged from ~13/s to 236/s (18x). Redo log growth accelerated from 14 KB/min to 756 KB/min (54x). Load average rose from 0.47 to 0.65. Likely a batch job or ETL process.

![Redo Log Growth](report_figures/06_redo_log_growth.png)

### 1.5 Other Issues

| Issue | Severity | Detail |
|-------|----------|--------|
| 3.8% IO wait | Medium | Persistent disk contention; will be worse during write bursts |
| Buffer pool hit rate dips to 993/1000 | Medium | Pool 1 and 3 dropped at 01:19; undersized for working set |
| 105 monitoring errors in 7 min | Medium | SSH auth failures and network errors in trace script -- systematic, not transient |
| Stale CPU data | Low | Identical CPU values across all snapshots -- `top -bn1` reports boot averages, not real-time |

---

## 2. Primary QPS Drivers

![QPS Analysis](report_figures/05_qps_analysis.png)

### 2.1 192.0.2.14 -- 43% of all queries (dominant driver)

Generates exactly 22 queries in every snapshot, never varying. Uses accounts `beappro` and `app_user`. This rigid pattern indicates a **fixed 22-connection pool** where every connection perpetually holds an active query -- characteristic of a polling loop or an application that never releases connections.

### 2.2 192.0.2.12 -- 10% of queries (bursty)

Highly variable: 0, 2, **11**, 5, 5, 0, 2, 10. The spike to 11 at 01:20 caused the only QPS surge (48 -> 60 total). Pattern suggests a **batch-oriented workload** -- reporting, aggregation, or periodic sync.

### 2.3 203.0.113.10 -- 6% of queries (declining)

Started at 6 queries, dropped to 3, then 1, then 0. A process completing its work or a service draining connections.

### 2.4 Background clients -- ~31% combined

12+ IPs maintaining 1-3 constant queries each. Service-to-service connections using `svc_user` and `dbuser` -- health checks, session management, persistent read connections.

### QPS by Snapshot

| Time | Total | 192.0.2.14 | 192.0.2.12 | 203.0.113.10 | Others |
|------|-------|-----------|-----------|-------------|--------|
| 01:18 | 48 | 22 (46%) | 0 | 6 | 20 |
| 01:19 | 48 | 22 (46%) | 2 | 6 | 18 |
| 01:20 | **60** | 22 (37%) | **11** | 6 | 21 |
| 01:21 | 49 | 22 (45%) | 5 | 3 | 19 |
| 01:22 | 50 | 22 (44%) | 5 | 1 | 22 |
| 01:23 | 42 | 22 (52%) | 0 | 0 | 20 |
| 01:24 | 43 | 22 (51%) | 2 | 0 | 19 |

---

## 3. Errors

![Errors](report_figures/07_errors.png)

| Error Type | Count | Affected Hosts | Frequency |
|-----------|-------|---------------|-----------|
| Network error (trace skip) | 54 | 198.51.100.14, .16, .10 | Every snapshot |
| Password prompt failure | 26 | 192.0.2.12, 192.0.2.10 | Every snapshot |
| Process no longer exists | 13 | 192.0.2.24 | Intermittent |
| Process ID syntax error | 12 | Various | Intermittent |

These are monitoring script failures, not MySQL errors. Broken SSH keys and network ACLs cause the same hosts to fail every snapshot.

---

## 4. Optimization Opportunities

| # | Problem | Action | Detail |
|---|---------|--------|--------|
| 1 | Purge stalled | Find and kill blocking transaction | `SELECT * FROM information_schema.INNODB_TRX ORDER BY trx_started LIMIT 1;` -- kill if idle. Increase `innodb_purge_threads` to 4-8. Set `innodb_max_purge_lag` for back-pressure. |
| 2 | Memory at 96.6% | Consolidate mysqld instances | Shut down unnecessary PIDs (1744, 1775947, 29467). Right-size `innodb_buffer_pool_size` to 10-11 GB. Consider adding RAM. |
| 3 | 192.0.2.14 dominates QPS | Profile and optimize | Enable slow query log (`long_query_time = 0`), identify polling patterns, reduce connection pool from 22, switch to event-driven if polling. |
| 4 | Write burst at 01:24 | Schedule and tune | Move batch jobs off-peak. Increase `innodb_log_file_size`. Tune `innodb_io_capacity` to match disk throughput. Evaluate SSD/NVMe. |
| 5 | Buffer pool undersized | Reclaim memory | After consolidating instances, allocate freed RAM to buffer pool. Target hit rate: 999/1000. |
| 6 | Monitoring broken | Fix infrastructure | Repair SSH keys, verify network to 198.51.100.x, switch to `top -bn2`, add alerting thresholds. |

---

## Appendix: Reproducing This Analysis

```bash
pip install -r requirements.txt
python analyze_logs.py
# Outputs: console summary + 7 charts in report_figures/
```

---

*Analysis of Logs-sanitised-2026-04-17_For Assesment.csv*
