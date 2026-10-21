# Midnight daily monitor lane

All times are Asia/Shanghai. The existing scheduler, not a new cron/executor,
serializes these tasks starting at 00:00:

1. SpecDetailSeasonMonitor
2. WowDataVersionMonitor (existing isolated worker)
3. MythicDungeonToolsMonitor
4. MaxrollClassGuideMonitor
5. WowDailyReportMonitor
6. SpecDetailPlayerMonitor
7. SpecDetailRankingMonitor
8. SpecDetailAggregationMonitor

This is an ordered runnable queue, not fixed-hour spacing. No 07:00 cutoff or
running-task cancellation exists. Leases must complete/release/expire before
another lane task can claim. Existing heartbeat and exact-owner fencing remain.
No schema migration is needed: claimers already lock the complete small
MonitorTask configuration table in primary-key order, then MonitorTaskLease,
then SeasonMeta/cycle. Lane decisions are inside that same short transaction;
network/aggregation work remains outside it. Deployment does not clear leases,
cycles, error state or backoff.

Worker task_names filters apply AFTER determining the global lane head. A
normal worker cannot skip the WowData head, nor can the isolated worker jump
past the season task. Manual claims still bypass due-time/enable switches, but
cannot bypass the lane mutex. Disabled/environment-ineligible rows with live
leases still protect the lane. Legacy duplicate labels inherit lane membership
from their exact (type,target,env_limit) scope; existing scope canonicalization
is retained. Distinct non-lane targets/environments are not merged.

Unfinished WCL collection/publication gets first opportunity when runnable,
including after midnight. Retry delays are never cleared by a scheduler claim;
backoff does not occupy a lease after release, so other runnable daily work can
proceed. WCL daily identity and aggregation earliest gate both use midnight;
ready/collected evidence remains mandatory for WCL publication. A legacy 03:00
published cycle already represents its local day and is not duplicated as a
new 00:00 cycle. Retail central metadata publication can refresh ordinary
projections from the completed cohort. Missing/outdated burst snapshots do not
schedule or gate ordinary WCL aggregation. Neither the daily aggregator nor
`aggregate_spec_stats` (including legacy/manual replay) builds burst statistics;
burst updates belong to the independent, default-disabled entry point. Independent
player leaderboard generation retains its existing no-ready-cycle behavior;
it is not evidence of WCL aggregation success.

Default intervals for the lane are 86400. Fixed slot eligibility overrides
stored elapsed intervals; sync still preserves administrator Maxroll/WowData
interval values and all enable switches. last_scan_time remains a CLAIM marker,
not success evidence. Non-WCL daily tasks retain existing dispatch/retry
semantics; this change does not invent durable success records for them.

## Outside the heavy lane

Peak (600 seconds), DPS (3600 seconds), news and WagoSkillDiff retain existing
scheduling. They can claim while a heavy lane lease is live. WowToday retains
04:00/08:00/10:00 upstream freshness slots, outside the heavy mutex: delaying
it behind hours of player/WCL processing would defeat late-source refreshes.
It is not pulled forward to midnight. This does not guarantee spare executor
threads; the lane adds no executor capacity.

## Local verification boundary

`botend.tests.test_monitor_night_lane` exercises real ORM rows, lease owners,
worker routing, manual mutex, disabled aliases, environment gates, old cycles,
retry and early-ready publication. Its TransactionTestCase uses three threads
with independent DB connections and a barrier for normal/isolated/manual claims.
That test requires MySQL row locks and explicitly skips on SQLite; SQLite is
not concurrency proof. Use only host-asserted local isolated MySQL settings.
No production execution, lease cleanup, backfill, deployment or commit is part
of this implementation.
