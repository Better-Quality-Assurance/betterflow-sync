# Reliability audit: memory leaks and network drop handling

Date: 2026-03-03
Status: Pending fixes

Each issue below should be fixed in its own commit. Work through them top-down by priority.

---

## Memory issues (6 total)

### M1. Sent cache unbounded growth [CRITICAL]
- **File:** `src/sync/sync_engine.py` lines 97-98, 351-359
- **Problem:** `_sent_cache` dict tracks `(bucket_id, event_id)` pairs for dedup. Soft cap at 10K entries but eviction is lazy (only triggers on overflow check). Grows ~72K-144K entries/day before eviction fires.
- **Fix:** Replace with bounded LRU using `collections.OrderedDict`. Hard cap at 5K entries, evict on every insert.

### M2. Tray menu fully recreated on every state change [CRITICAL]
- **File:** `src/ui/tray.py` lines 198-393
- **Problem:** `_create_menu()` builds 30-50 MenuItem objects with closures on every `set_state()`, `set_active_time()`, `update_stats()`. Called ~1,440 times/day. pystray doesn't GC old menus immediately, causing GC pressure.
- **Fix:** Debounce menu recreation (max once per second). Or cache menu structure and only update changed text values.

### M3. Category cache not thread-safe [MEDIUM]
- **File:** `src/sync/sync_engine.py` lines 102, 172-174
- **Problem:** `_category_cache` lazily populated from SQLite. Race condition if two threads initialize simultaneously.
- **Fix:** Wrap cache init in `threading.Lock`.

### M4. OfflineQueue connection list never shrinks [MEDIUM]
- **File:** `src/sync/queue.py` lines 59-70
- **Problem:** `_connections` list appends thread-local SQLite connections but never removes them when threads die. Grows ~1-2 per session.
- **Fix:** Use weak references, or clean up stale connections in `close()`.

### M5. NSNotification observers never removed [MEDIUM]
- **File:** `src/system_events.py` lines 82-161
- **Problem:** macOS notification observers registered in `start_system_event_listener` but `removeObserver_` never called. Grows per login/logout cycle.
- **Fix:** Store observer refs, call `removeObserver_` in a cleanup method called from `_shutdown()`.

### M6. Display tracker threads rely on stop event only [LOW]
- **File:** `src/display_info.py` lines 147, 208
- **Problem:** Daemon threads properly stopped by `_shutdown()` but no fallback if exception prevents shutdown from running.
- **Fix:** Add try/finally in `_run()` to ensure observer cleanup.

---

## Network issues (15 total)

### N1. Mid-request drop causes event duplication [CRITICAL]
- **File:** `src/sync/sync_engine.py` lines 635-670
- **Problem:** If server processes events but connection drops before response arrives, client queues ALL events again. No request-level deduplication.
- **Fix:** Add idempotency key header to event POST requests. Server should deduplicate by request ID.

### N2. Queue corruption undetected after startup [CRITICAL]
- **File:** `src/sync/queue.py` lines 87-107
- **Problem:** `PRAGMA integrity_check` runs only at `__init__`. If SQLite corrupts mid-operation (power loss during write), queued events are silently lost.
- **Fix:** Run `PRAGMA quick_check` periodically (e.g. hourly) or on dequeue failure.

### N3. Stale TCP connections after laptop sleep [CRITICAL]
- **File:** `src/sync/http_client.py` lines 108, 256-260
- **Problem:** `requests.Session` connection pool holds dead TCP connections after sleep. First sync after wake waits 30s for stale connection timeout.
- **Fix:** Reset `self._session = requests.Session()` in `_on_system_wake()`. Or set `pool_maxsize` and connection TTL via `HTTPAdapter`.

### N4. Race: network change during active sync [MAJOR]
- **File:** `src/main.py` lines 626-638
- **Problem:** `_on_network_change(False)` calls `sync_engine.pause()` while a request may be in-flight. No coordination between network detector and active sync.
- **Fix:** Add `_sync_in_progress` flag. Don't interrupt in-flight sync; let it complete or timeout.

### N5. Checkpoint updated before send confirmation [MAJOR]
- **File:** `src/sync/queue.py` lines 367-389, `src/sync/sync_engine.py`
- **Problem:** If crash occurs after checkpoint update but before events confirmed sent, overlap window causes duplicates on restart.
- **Fix:** Move `set_checkpoint()` to AFTER `send_events()` succeeds.

### N6. AW health check timeout too high [MAJOR]
- **File:** `src/sync/aw_client.py` line 115
- **Problem:** Single 10s timeout for all AW requests. Health check (`is_running()`) should fail fast (3s) but waits the full 10s.
- **Fix:** Use 3s timeout for `is_running()`, keep 10s for event queries.

### N7. Retry count grows past max between cleanup runs [MAJOR]
- **File:** `src/sync/queue.py` lines 231-249
- **Problem:** `remove_failed()` only runs daily via scheduler. Between runs, events keep being retried past max count of 5.
- **Fix:** Check retry count in `dequeue()` and skip over max-retried events. Or remove immediately in `increment_retry()` when max reached.

### N8. DNS failures retried excessively [MEDIUM]
- **File:** `src/sync/http_client.py` line 203
- **Problem:** DNS resolution failures caught as `ConnectionError` and retried 3x, wasting 7s+ on unrecoverable DNS.
- **Fix:** Catch `socket.gaierror` separately and don't retry.

### N9. Partial response (ChunkedEncodingError) not caught [MEDIUM]
- **File:** `src/sync/http_client.py` lines 187-215
- **Problem:** If connection drops mid-response body, `response.json()` raises `ChunkedEncodingError` which is not caught. Events may be lost.
- **Fix:** Add explicit catch for `ChunkedEncodingError`, `ContentDecodingError`. Treat as transient, retry.

### N10. Network poller race on startup [MEDIUM]
- **File:** `src/system_events.py` lines 357-374
- **Problem:** 5s delay before first network poll. App may sync before knowing network state.
- **Fix:** Do immediate poll on startup before the 5s wait loop.

### N11. Malformed partial-success response causes duplication [MEDIUM]
- **File:** `src/sync/sync_engine.py` lines 641-670
- **Problem:** If server returns partial success without `accepted_ids` field, entire batch is re-queued.
- **Fix:** Require `accepted_ids` in response contract. Log warning if missing.

### N12. 429 rate limit not retried with backoff [LOW]
- **File:** `src/sync/http_client.py` lines 191-200
- **Problem:** 429 (Too Many Requests) treated as non-retryable 4xx error. Should use `Retry-After` header.
- **Fix:** Treat 408, 429, 503, 504 as retryable. Read `Retry-After` header for 429.

### N13. Session start/end not retried [LOW]
- **File:** `src/sync/sync_engine.py` lines 219-224
- **Problem:** `start_session()` and `end_session()` fail silently on network error. Session state may be inconsistent.
- **Fix:** Retry session calls with backoff, or make them idempotent server-side.

### N14. Heartbeat timeout too long [MEDIUM]
- **File:** `src/sync/bf_client.py` lines 258-268
- **Problem:** Heartbeat uses default retry (3x with backoff), can block 30s+ if server slow.
- **Fix:** Use `max_retries=1, timeout=5` for heartbeat specifically.

### N15. Retry jitter can reduce delay below previous attempt [LOW]
- **File:** `src/sync/retry.py` lines 56-60
- **Problem:** `uniform(-jitter_range, jitter_range)` can make retry delay shorter than previous attempt. Cosmetic issue.
- **Fix:** Use `uniform(0, jitter_range)` for monotonically increasing delays.

---

## Recommended fix order

**Session 1 - Critical memory:** M1 (sent cache LRU) + M2 (menu debounce)
**Session 2 - Critical network:** N3 (stale TCP on wake) + N9 (ChunkedEncodingError) + N8 (DNS fast-fail)
**Session 3 - Data integrity:** N5 (checkpoint ordering) + N1 (idempotency key) + N2 (periodic integrity check)
**Session 4 - Major reliability:** N4 (sync-in-progress flag) + N6 (AW timeout) + N7 (retry cap in dequeue)
**Session 5 - Medium cleanup:** M3 (thread-safe cache) + M5 (observer cleanup) + N10 (startup poll) + N14 (heartbeat timeout)
**Session 6 - Low priority:** M4, M6, N11, N12, N13, N15
