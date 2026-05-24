# Assessment: MySQL DB-Master Log Analysis

**Log:** Logs-sanitised-2026-04-17_For Assesment.csv (5,344 lines)  
**Host:** svc-db-node-001.internal.example-corp.com (db-master)  
**Window:** 2026-04-16 01:18--01:25 UTC (8 snapshots at ~1-minute intervals)

---

## Part 1: Issues Observed

### Issue 1 — InnoDB Purge Thread is Stalled (Critical)

The purge thread has not advanced across any of the 7 InnoDB status snapshots. The purge LSN remains frozen at transaction 5,845,989,957 while the transaction counter moved from 5,846,029,499 to 5,846,038,526 -- a growing gap of ~48,500 unpurged transactions. As a direct consequence, the **history list length is growing monotonically** from 4,735 to 4,859 (+124 in 7 minutes). Healthy systems maintain a history list below 1,000.

This strongly indicates a **long-running or abandoned transaction** is blocking the purge thread. Old row versions accumulate in the undo tablespace, bloating disk usage, degrading read performance (longer MVCC chains to traverse), and polluting the buffer pool with undo pages.

### Issue 2 — Severe Memory Pressure (Critical)

The server uses 14.2 GB of 14.7 GB total RAM (96.6%), leaving only ~500 MB free. While swap usage is minimal (~97 MB / 8 GB), the system has no headroom. Any memory spike -- from a connection burst, large sort, or temp table -- could trigger swap thrashing, which would catastrophically degrade database performance since disk access is 100-1000x slower than RAM.

### Issue 3 — Four mysqld Processes on a Single Host (High)

Four separate mysqld daemons are running concurrently:

| PID | CPU % | Running Since | Cumulative CPU |
|-----|-------|---------------|---------------|
| 4374 | 38% | 2025 (111+ days) | 111d+ |
| 1744 | **59%** | Apr 05 (~11 days) | 6d 9h |
| 1775947 | 36% | Apr 13 (~3 days) | 18h 10m |
| 29467 | 13% | Apr 15 (~1 day) | 3h 23m |

Combined CPU: 146%. Each instance maintains its own buffer pool, so instead of one process efficiently using ~12 GB of cache, four processes each get a fraction. The staggered start dates (2025, Apr 5, Apr 13, Apr 15) suggest either orphaned processes from failed restarts or intentionally co-located instances -- both are problematic on a memory-constrained server.

### Issue 4 — I/O Write Spike at 01:24 (High)

InnoDB write rates surged from ~13/s to 236/s (an 18x increase) and reads from ~47/s to 255/s. The redo log growth rate confirms this: LSN delta accelerated from 14 KB/min to 756 KB/min (54x increase). This correlates with the load average ticking up from 0.47 to 0.65. A batch job, ETL process, or bulk data operation is the most likely cause.

### Issue 5 — Persistent 3.8% IO Wait (Medium)

CPU IO-wait is 3.8% across all snapshots (noting that these are cumulative averages from `top -bn1`, not real-time values). For a database server, sustained IO wait above 2-3% signals the storage subsystem is a bottleneck, particularly during the write bursts identified above.

### Issue 6 — Buffer Pool Hit Rate Dips (Medium)

Some buffer pool instances drop to 993-994/1000 at 01:19. While above 99%, for an OLTP database this means 6-7 out of every 1,000 page accesses hit disk instead of RAM. With thousands of reads per second, this translates to hundreds of additional disk I/Os. The dips likely correspond to cold-page reads from a scan or batch query evicting hot data.

### Issue 7 — Monitoring Script Failures (Medium)

105 errors in 7 minutes from the monitoring tool itself:
- 54 network errors (skipped traces for 198.51.100.14, .16, .10)
- 26 password prompt failures (192.0.2.12, 192.0.2.10)
- 13 "process no longer exists" errors
- 12 process ID syntax errors

These are systematic, not transient -- the same hosts fail every snapshot. The monitoring infrastructure is misconfigured (broken SSH keys, network ACL issues), creating noise that obscures real problems.

### Issue 8 — Stale CPU Monitoring Data (Low)

CPU percentages (18.5% user, 2.4% sys, 75.1% idle, 3.8% wa) are identical across all 8 snapshots. This is statistically impossible in real-time and indicates the monitoring script uses `top -bn1` (single-iteration batch mode), which reports cumulative averages since boot rather than current values. The real-time CPU profile could be significantly different.

---

## Part 2: Primary Drivers Behind QPS

### Driver 1 — 192.0.2.14 (43% of all queries)

This is the **dominant QPS driver**. It generates exactly 22 queries in every single 1-minute snapshot, never varying. It connects using two user accounts (`beappro` and `app_user`) and holds the most MySQL threads of any client.

**Why exactly 22 every time?** This pattern indicates a **fixed-size connection pool of 22 connections** where every connection perpetually holds an active query. This is characteristic of:
- A polling loop (e.g., repeatedly querying a job/task queue table)
- An application that never releases connections back to the pool
- Persistent connections with continuous request serving

**Impact:** At 22/48 queries per snapshot = 46% of total QPS, this single client is the largest contributor to database load.

### Driver 2 — 192.0.2.12 (10% of queries, highly variable)

This IP shows the most volatility: 0 queries at 01:18, then 2, 11, 5, 5, 0, 2, 10 across subsequent snapshots. The spike to 11 at 01:20 correlates with the only QPS surge in the observation window (total queries jumped from 48 to 60). This bursty pattern suggests a **batch-oriented workload** -- possibly a reporting service, data aggregation, or periodic sync job.

### Driver 3 — 203.0.113.10 (6% of queries, declining)

Started at 6 queries per snapshot, dropped to 3 at 01:21, then 1 at 01:22, and disappeared. This declining pattern could indicate a process completing its work, a connection pool draining, or a service being stopped/restarted.

### Driver 4 — Steady-state background clients

Multiple IPs (192.0.2.23, 192.0.2.10, 192.0.2.13, 192.0.2.24) maintain 1-3 constant queries per snapshot. These represent **service-to-service connections** (using `svc_user` and `dbuser` accounts) -- likely application servers with health checks, session management, or persistent read replicas. Individually small, they collectively account for ~30% of QPS.

### QPS Summary by Snapshot

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

## Part 3: Potential Problems and Optimization Opportunities

### Problem 1: Purge Stall Will Progressively Degrade Performance

**The problem:** The growing history list (4,735 -> 4,859 in 7 minutes, ~18/min) is a ticking bomb. If the blocking transaction isn't resolved:
- Undo tablespace will grow on disk and may not auto-shrink
- SELECT queries will slow down as MVCC version chains lengthen
- Buffer pool efficiency will drop as undo pages consume cache

**Optimization:** Identify and resolve the blocking transaction:
```sql
SELECT trx_id, trx_started, trx_mysql_thread_id, trx_state, trx_query
FROM information_schema.INNODB_TRX
ORDER BY trx_started ASC LIMIT 1;
```
If the oldest transaction is idle, `KILL` its thread ID. Then increase `innodb_purge_threads` from default to 4-8 and set `innodb_max_purge_lag` to add back-pressure when purge falls behind.

### Problem 2: Memory Fragmentation Across 4 Instances

**The problem:** 4 mysqld processes each allocate their own buffer pool, connection buffers, and internal structures. If each gets roughly equal share of 14.7 GB, they each have ~3.5 GB of buffer pool instead of one instance with ~11 GB. Smaller buffer pools = more cache misses = more disk I/O.

**Optimization:** Consolidate to a single mysqld instance. Shut down PIDs 1744, 1775947, and 29467 after confirming they're not serving critical traffic. Right-size the remaining instance's `innodb_buffer_pool_size` to 10-11 GB (70-80% of total RAM).

### Problem 3: 192.0.2.14 Connection Pool Behavior

**The problem:** 22 permanently active queries from one client consumes thread resources, holds locks longer than necessary, and generates ~46% of QPS. If these are polling queries, the database is doing redundant work.

**Optimization opportunities:**
- **Profile the queries:** Enable slow query log with `long_query_time = 0` temporarily to capture what 192.0.2.14 is actually running
- **If polling:** Replace `SELECT ... WHERE status='pending'` polling with event-driven patterns (MySQL event scheduler, application-side message queues, or `SLEEP()`-based long polling)
- **Connection pool tuning:** Reduce pool size from 22 to match actual concurrency needs. Set idle timeout to release unused connections

### Problem 4: Write Burst Causing I/O Saturation

**The problem:** The 18x write spike at 01:24 (13/s -> 236/s) combined with 3.8% baseline IO wait means disk I/O is saturated during bursts. If the redo log fills faster than pages can be flushed, InnoDB will stall all DML operations until flushing catches up (`innodb_log_waits` > 0).

**Optimization opportunities:**
- Schedule heavy write jobs (ETL, batch imports) during off-peak hours
- Increase `innodb_log_file_size` to absorb write bursts without triggering checkpoint storms
- Tune `innodb_io_capacity` and `innodb_io_capacity_max` to match actual disk throughput
- Evaluate storage upgrade to SSD/NVMe if currently on HDD
- Separate redo log files and data files onto different physical disks

### Problem 5: Buffer Pool Undersized for Working Set

**The problem:** Hit rate dips to 993/1000 on some pool instances mean the frequently-accessed data doesn't fully fit in RAM. Every cache miss is a disk read.

**Optimization:** After consolidating mysqld instances (Problem 2), the freed memory should be allocated to the buffer pool. Monitor `Innodb_buffer_pool_reads` (physical disk reads) and `Innodb_buffer_pool_read_requests` (logical reads) to track improvement. Target: sustained 999/1000 or better.

### Problem 6: Monitoring Blind Spots

**The problem:** The monitoring script fails silently for multiple hosts every cycle (105 errors in 7 minutes). This means connection traces from 198.51.100.14, .16, .10, 192.0.2.12, and 192.0.2.10 are never collected -- creating blind spots in observability.

**Optimization:**
- Fix SSH key authentication for the monitoring script on all target hosts
- Verify network connectivity and firewall rules for 198.51.100.x hosts
- Fix the `top -bn1` issue by switching to `top -bn2` (discard first iteration for accurate real-time CPU)
- Add alerting thresholds so issues are flagged automatically, not found by manual log review

---

## Visualization

Charts and detailed data tables supporting this analysis are available in the full report:
- **[Full Report with Charts](DB_Master_Log_Analysis_Report.md)**
- Charts generated by `analyze_logs.py` in the `report_figures/` directory
