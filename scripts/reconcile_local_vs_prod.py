#!/usr/bin/env python3
"""Completeness check: is the agent uploading everything it should (no stall)?

The stall this guards against (idle-gap checkpoint pin, fixed in v1.5.69) leaves a
CONTIGUOUS span where the local AW store has activity but prod received nothing.
That is the only "we'd need backfill" signal. It is NOT the same as a 1:1 event
match: the agent legitimately drops window events shorter than
min_window_event_seconds (default 5s), gap-fills, dedups and transforms — so a
naive per-event diff always shows benign differences.

So this compares COVERAGE per time bucket: for each bucket where the local store
has *sendable* activity, prod must also have events. A bucket that is local-active
but prod-empty is a hole (a stall). Run it any time — especially after an idle
gap / "after an hour" — to confirm the live sync kept prod complete:

    MYSQL_PWD=... DEVICE_ID=14 python3 scripts/reconcile_local_vs_prod.py [minutes]

Exit 0 = complete (no holes), exit 1 = holes found.
"""
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOOKBACK_MIN = int(sys.argv[1]) if len(sys.argv) > 1 else 120
SETTLE_SECONDS = 180          # ignore the last 3 min (in-flight / next sync cycle)
BUCKET_SECONDS = 300          # 5-min coverage buckets
MIN_WINDOW_SECONDS = 5        # agent filters window events shorter than this
DEVICE_ID = os.environ.get("DEVICE_ID", "14")
# Connection comes entirely from the environment — no prod host/credentials are
# committed. Set DB_HOST/DB_PORT/DB_NAME and MYSQL_PWD before running.
DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = os.environ.get("DB_PORT", "3306")
DB_NAME = os.environ.get("DB_NAME", "betterflow")
AW_DB = Path.home() / "Library/Application Support/BetterFlow/data/aw-db.sqlite"

# bucketrow -> (label, prod event_type, min_duration_to_send)
STREAMS = {1: ("window", "window", MIN_WINDOW_SECONDS), 2: ("input", "input", 0)}

now = datetime.now(timezone.utc)
end = now - timedelta(seconds=SETTLE_SECONDS)
start = end - timedelta(minutes=LOOKBACK_MIN)
start_s, end_s = int(start.timestamp()), int(end.timestamp())


def _local_buckets(bucketrow, min_dur):
    con = sqlite3.connect(f"file:{AW_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT (starttime/1000000000)/? , COUNT(*) "
            "FROM events WHERE bucketrow=? AND (endtime-starttime)/1000000000 >= ? "
            "AND starttime>=? AND starttime<? GROUP BY 1",
            (BUCKET_SECONDS, bucketrow, min_dur, start_s * 1_000_000_000, end_s * 1_000_000_000),
        ).fetchall()
        return {int(b): c for b, c in rows}
    finally:
        con.close()


def _prod_buckets(event_type):
    sql = (
        f"SELECT FLOOR(UNIX_TIMESTAMP(event_timestamp)/{BUCKET_SECONDS}), COUNT(*) "
        f"FROM agent_events WHERE device_id={DEVICE_ID} AND event_type='{event_type}' "
        f"AND event_timestamp>=FROM_UNIXTIME({start_s}) AND event_timestamp<FROM_UNIXTIME({end_s}) "
        f"GROUP BY 1;"
    )
    out = subprocess.run(
        ["mysql", "-h", DB_HOST, "-P", DB_PORT, "-u", "root", "-N",
         "--connect-timeout=10", DB_NAME, "-e", sql],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise SystemExit(f"prod query failed: {out.stderr.strip()}")
    res = {}
    for ln in out.stdout.splitlines():
        b, c = ln.split("\t")
        res[int(b)] = int(c)
    return res


def _hhmm(bucket):
    return datetime.fromtimestamp(bucket * BUCKET_SECONDS, timezone.utc).strftime("%H:%M")


print(f"Coverage reconcile device {DEVICE_ID}: {start.strftime('%H:%M')}–{end.strftime('%H:%M')} UTC "
      f"(last {LOOKBACK_MIN}m, {BUCKET_SECONDS // 60}-min buckets, settled to -{SETTLE_SECONDS}s)\n")

ok = True
for row, (label, prod_type, min_dur) in STREAMS.items():
    loc = _local_buckets(row, min_dur)
    pro = _prod_buckets(prod_type)
    holes = sorted(b for b in loc if loc[b] > 0 and pro.get(b, 0) == 0)
    status = "PASS" if not holes else "FAIL"
    if holes:
        ok = False
    print(f"[{status}] {label:7s}  active buckets={len(loc):3d}  holes={len(holes)}")
    for b in holes:
        print(f"          - {_hhmm(b)} UTC: local={loc[b]} sendable, prod=0  <-- STALL")

print("\nRESULT:", "COMPLETE — no local-active/prod-empty buckets; agent uploaded everything ✅"
      if ok else "STALL — local activity missing from prod; backfill would be needed ❌")
sys.exit(0 if ok else 1)
