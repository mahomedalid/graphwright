---
title: Application Hang Investigation and Remediation Handoff
description: Reproduced SQLite deadlock, cancellation gaps, lock recovery defects, and verification criteria.
ms.date: 2026-10-28
ms.topic: troubleshooting
---

## Summary

Concurrent discovery tools can deadlock the shared SQLite connection. This failure
was reproduced twice using the application's actual adapter and synthetic data.
It is the strongest explanation for the reported application hang requiring
`kill -9`, although no stack dump from the user's incident was available to prove
that the same failure occurred there.

The earlier iteration limits, logging, model timeout, and reduced UI polling do
not prevent this deadlock. An asyncio timeout cannot terminate a synchronous
worker thread, and the deadlock can also obstruct Python signal handling.

Status: HANG-001 remediation implemented. Foundry discovery tools now run
sequentially, and a per-adapter lock protects SQLite execution, progress-handler
changes, metadata reads, and close. Cursors are closed before releasing the lock.
Subprocess regression tests cover overlapping queries, metadata reads, and close.
The remaining findings are not fixed by this change.

Related requirements: [Data Discovery Graph PRD](prds/data-discovery-graph.md).
This handoff does not change or approve the PRD.

## Evidence and scope

The review followed run execution, Agent Framework tool dispatch, SQLite
connection access, cancellation, topology cache locking, and server lifecycle.
Reproductions used isolated child processes, temporary synthetic databases, and
no Foundry or Databricks network calls.

Environment used for the deadlock reproduction:

* Windows with Python 3.12.10
* `agent-framework-core` 1.22.0
* `agent-framework-foundry` 1.15.0
* The repository's current SQLite adapter

The user's deployment may use a different OS or SDK version. Confirm those
versions when validating the fix in the deployment environment.

Recent persisted telecom runs included records still marked `running`, with
their last events showing discovery tools such as `describe_table`. This is
consistent with an interruption or stalled tool, but does not independently
prove a deadlock. No candidate or topology lock files were present during the
inspection. Older events also predated the latest observability changes.

## Findings

### HANG-001: concurrent SQLite tools can hard-deadlock the process

Severity: High. Evidence: reproduced twice.

Relevant code:

* [SQLite connection creation and query execution](../src/ddg/adapters/sqlite_adapter.py)
* [Plain synchronous tool wrappers and Foundry configuration](../src/ddg/agent/foundry_planner.py)
* [Shared discovery-tool state](../src/ddg/tools.py)

The adapter creates one connection with `check_same_thread=False`. Each
`_execute()` call installs a Python progress handler, executes SQL, and removes
the handler in `finally`. There is no application-level serialization of those
operations in the reviewed implementation. The remediation now serializes these
operations with a per-connection lock.

The installed Agent Framework invokes ordinary synchronous functions through
`asyncio.to_thread()`. Its tool dispatcher defaults
`allow_concurrent_invocation` to `True`. The application's
`function_invocation_configuration` did not override that default before the
remediation; it now explicitly sets `allow_concurrent_invocation=False`.

As a result, a single model response containing multiple tool calls can execute
them concurrently against the same adapter. Disabling SQLite's thread-affinity
check does not make this connection/progress-callback lifecycle safe for
concurrent use.

The observed stacks support a SQLite mutex/Python global interpreter lock
(GIL) inversion:

1. Worker A executes SQL on the shared connection with a Python progress callback.
2. Worker B attempts to install another progress handler on that connection.
3. Changing the handler waits on SQLite's connection mutex while Python
   execution can retain the GIL.
4. Worker A needs the GIL to enter its progress callback before it can finish
   and release the SQLite mutex.
5. Neither worker can advance; the query timeout itself depends on that callback.

The stack dumps identify the blocked operations, not internal C mutex ownership.
The lock-inversion mechanism is an explanation consistent with the stacks and
serialized control result.

#### Reproduction results

Both concurrent attempts used a one-second query timeout but exceeded the
four-second child-process limit and required termination. Faulthandler captured:

```text
Worker B: SQLiteAdapter._execute, sqlite_adapter.py:27
          self._conn.set_progress_handler(...)
Worker A: SQLiteAdapter._execute, sqlite_adapter.py:29
          cursor = self._conn.execute(sql)
Main thread: asyncio executor submission / worker startup
```

Serializing the same queries completed in about 1.52 seconds with exit code 0:

```text
['TimeoutError', 'tuple']
```

The expensive query timed out as intended; the following `SELECT 1` succeeded.
This rules out query cost alone as the explanation for the concurrent hang.

#### Recommended correction

1. Set `allow_concurrent_invocation=False` in the Foundry function-invocation
   configuration. Verify the option against the installed SDK.
2. Serialize all access to each SQLite connection, including progress-handler
   changes, SQL execution/fetching, metadata operations, and close. A lock must
   be acquired before entering SQLite, not after installing the callback.
3. Keep tool execution serialized at the planner boundary because shared
   `DiscoveryTools` and usage state are not designed as concurrent state.
4. Review lock acquisition order and avoid nested acquisition of a
   non-reentrant connection lock.

Changing only the query progress handler or setting
`check_same_thread=False` again is not a fix. Protecting only `_execute()` also
leaves metadata and connection-close paths outside the safety boundary.

### HANG-002: asyncio deadlines do not terminate synchronous worker threads

Severity: High. Evidence: shutdown delay reproduced; indefinite delay follows
when the worker has the HANG-001 deadlock.

Relevant code:

* [Iteration asyncio entry point and middleware](../src/ddg/agent/foundry_planner.py)
* [Model timeout context](../src/ddg/control.py)
* [Run finalization](../src/ddg/agent/runner.py)

`FoundryPlanner.run_iteration()` calls `asyncio.run()`. The SDK offloads
synchronous tools into the loop's default executor. Cancelling the awaiting
coroutine does not stop a function already executing in that executor.

Python 3.12's asyncio runner waits for its default executor during shutdown,
with a five-minute executor join timeout. Timing out that join does not kill a
blocked thread; executor threads can also obstruct interpreter exit.

A bounded demonstration using `asyncio.to_thread(time.sleep, 0.8)` produced:

```text
Timeout raised after 0.05
asyncio.run returned after 0.81
```

Therefore a timeout event is not evidence that the underlying work has stopped.
Neither `daemon=True` on the outer discovery thread nor an async task timeout
provides a hard kill boundary for SDK executor workers.

Recommended correction:

* Make adapters enforce deadlines and cancellation inside their blocking work.
* Ensure connection cleanup occurs only after its work has stopped.
* If a strict upper bound is required even for non-cooperative drivers, isolate
  run execution in a managed process with checkpointing and bounded termination.
  Treat this as a deliberate architecture decision, not a replacement for the
  SQLite fix.
* Do not mark a run successfully finished merely because its async waiter was
  cancelled while its tools can still mutate persisted state.

### HANG-003: orphan topology locks block restarted runs

Severity: Medium. Evidence: confirmed control flow and synthetic lock test;
not observed as the active lock state during inspection.

Relevant code:

* [Topology parsing lock](../src/ddg/topology.py)
* [File-lock implementation](../src/ddg/store.py)

Topology parsing holds a PID-bearing lock file across the Foundry network call.
Its stale threshold is at least 3,600 seconds. The lock implementation checks
file age, not whether the owning process still exists.

After forced termination during parsing, a restarted run can spend its entire
remaining run budget waiting on the orphan. The wait loop sleeps without
checking cancellation. A synthetic lock with a nonexistent PID timed out after
approximately 0.36 seconds with a requested 0.3-second acquisition timeout,
rather than being reclaimed.

The shared lock helper also unlinks by pathname on release and can delete an
old lock based only on age. Any fix must prevent one owner from removing
another owner's replacement lock.

Recommended correction:

* Use an OS-backed lock released on process death, or implement verified
  ownership and safe stale-owner recovery.
* Account for PID reuse if PID-based ownership remains.
* Make acquisition waits check cancellation and the run deadline.
* Report lock waiting separately from model execution and return an explicit
  acquisition failure.
* Never delete all lock files on startup or reclaim a live owner's lock based
  only on elapsed time.

### HANG-004: cancellation and query budgets are cooperative and incomplete

Severity: Medium. Evidence: confirmed control flow.

Relevant code:

* [Cancellation checkpoints and model wait](../src/ddg/control.py)
* [Databricks polling and chunk retrieval](../src/ddg/adapters/databricks_adapter.py)
* [API run threads and cancellation endpoints](../src/ddg/api.py)
* [Server startup](../src/ddg/cli.py)

Cancellation is checked before work dispatch, not continuously during an active
model request or synchronous source operation. The API starts daemon run
threads but has no explicit lifespan handler coordinating their cancellation
and bounded shutdown.

Databricks has additional deadline gaps:

* Initial submission can use a subprocess timeout longer than the query budget.
* Status polls use a fixed 60-second timeout rather than the remaining budget.
* Result-chunk retrieval has no overall deadline check.
* Remote cancellation can add another 30-second wait.

These are long-wait paths, distinct from the reproduced SQLite hard deadlock.

Recommended correction:

* Propagate one monotonic deadline and a cancellation signal into adapter work.
* Bound each network/subprocess operation by the remaining budget.
* Check the deadline before and after submission, polling, and chunk retrieval.
* Interrupt SQLite work using an appropriately coordinated connection interrupt
  or progress callback once connection ownership is safe.
* Add server lifecycle handling that requests cancellation and performs bounded
  cleanup without holding the registry lock.
* Distinguish cancellation, run-budget expiration, upstream timeout, and cleanup
  failure in persisted status and events.

## Implementation sequence

1. Fix HANG-001 first with planner-level serialization and connection-level
   protection. Avoid unrelated adapter or storage refactoring.
2. Add the subprocess deadlock regression before treating the fix as complete.
3. Add cooperative cancellation and deadlines across active operations.
4. Repair topology lock recovery and cancellation-aware acquisition.
5. Add bounded API shutdown and verify process exit.
6. Decide separately whether strict execution deadlines warrant process
   isolation.

Do not treat slower polling, more logging, fewer iterations, or a shorter HTTP
timeout as substitutes for these changes.

## Regression tests and acceptance criteria

### SQLite concurrency regression

Run the reproduction in a child process, never directly in the pytest process.
A deadlock involving the GIL can prevent an in-process test timeout from firing.
The parent must terminate and reap only its own child if its deadline expires.

Reproduction recipe:

1. Create a temporary SQLite database with `numbers(n INTEGER)` and 2,000 rows.
2. Open `SQLiteAdapter(..., query_timeout_seconds=1)`.
3. Use `asyncio.to_thread()` to execute:

   ```sql
   SELECT sum(a.n * b.n * c.n)
   FROM numbers a, numbers b, numbers c;
   ```

4. After 0.05 seconds, dispatch `SELECT 1` through the same adapter in a second
   worker.
5. Enable `faulthandler.dump_traceback_later(1.5)` in the child.
6. Have the parent enforce a four-second limit for this synthetic case.
7. Retain the serialized case as a control.

After the fix, the child must exit normally, the expensive query must terminate
with its intended timeout, and the short query must succeed. Repeat the test to
exercise scheduling variation. Also cover simultaneous metadata access and
coordinated close. Adjust timing only with measured CI evidence, not to hide a
remaining hang.

### Planner and cancellation regressions

* Feed a mocked model response containing multiple tools and verify a maximum
  of one active discovery tool per run.
* Verify cancellation during a model wait, active SQLite query, Databricks poll,
  and result-chunk fetch.
* Verify no tool continues persistence after the run reports terminal status.
* Verify usage accounting and evidence remain correct when a tool times out.
* Verify run finalization records failures even when adapter cleanup fails.

### Lock and shutdown regressions

* Kill an isolated lock-owning child and verify the next owner can acquire the
  lock without waiting for the one-hour age threshold.
* Verify a live owner is not displaced and an old owner cannot unlink a
  replacement lock.
* Cancel a run waiting for topology parsing and verify prompt exit without
  calling Foundry.
* Stop the API with in-flight work and verify bounded process exit with no
  leftover executor workers or falsely completed runs.
* Verify another workspace remains responsive while one workspace initializes
  or waits for storage.

Use the existing test runner and dependencies. Existing async timeout mocks and
UI polling tests do not exercise SQLite mutex contention or process shutdown.

## Diagnostic evidence for any remaining incident

Capture a process-wide thread dump while the application is stuck. Include the
Python/SQLite/Agent Framework versions, run ID, source type, latest operational
event types, and whether the hang occurred during discovery, cancellation, or
server shutdown.

Use a scheduled faulthandler dump or another approved process diagnostic method.
Do not rely only on an HTTP diagnostic route, since a GIL-related deadlock can
prevent that route from responding.

Do not include credentials, environment-file contents, prompts, sampled rows,
or raw business data in the diagnostic bundle.

## Completion gate

The incident is not resolved until the SQLite subprocess reproduction passes
persistently, cancellation and process-exit behavior are tested, and the
deployment is exercised with its actual SDK and Python versions. A live Foundry
smoke test should follow local regressions, but no live cloud verification was
performed during this investigation.
