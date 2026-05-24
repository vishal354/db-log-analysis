# MySQL DB-Master Log Analysis

Analysis of a production MySQL database server's process list logs, identifying performance issues, InnoDB health problems, and QPS drivers.

## Report

**[View the full report](DB_Master_Log_Analysis_Report.md)**

## Key Findings

| # | Issue | Severity |
|---|-------|----------|
| 1 | InnoDB purge stalled -- history list growing | Critical |
| 2 | Memory at 96.6% (500 MB free of 14.7 GB) | Critical |
| 3 | 4 mysqld processes on one host | High |
| 4 | Single IP (192.0.2.14) generates 43% of queries | High |
| 5 | I/O write spike at 01:24 (18x increase) | High |
| 6 | Monitoring script errors (105 in 7 min) | Medium |

## Running the Analysis

```bash
pip install -r requirements.txt
python analyze_logs.py
```

This parses the log CSV and generates:
- A text summary with flagged issues printed to console
- 7 PNG charts saved to `report_figures/`

## Files

```
logs/                              # Source log data
  Logs-sanitised-2026-04-17_For Assesment.csv
analyze_logs.py                    # Parser + chart generator
report_figures/                    # Generated charts (7 PNGs)
DB_Master_Log_Analysis_Report.md   # Full analysis report
requirements.txt                   # Python dependencies
```
