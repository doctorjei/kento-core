#!/usr/bin/env python3
"""Live M6/M5 check for the typed Instance.start()/stop() on a REAL guest.

Exercises the typed lifecycle end-to-end against a real running instance,
through the (Block-09-modified) stop.py graceful path (LXC `lxc-stop --nokill`).
The stubborn-guest -> StopTimeout path is covered by unit tests + the Editor's
mechanism verification; here we validate the common cooperative path live:
RUNNING -> stop() -> STOPPED -> start() -> RUNNING -> stop(force=True) -> STOPPED.

Usage:  <python with kento on path> live-m6.py <instance-name>
Assumes the named instance already exists and is RUNNING (created via the CLI).
"""
from __future__ import annotations

import sys
import time

import kento

name = sys.argv[1]
_fail = 0


def check(label, cond, detail=""):
    global _fail
    ok = bool(cond)
    _fail += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  — {detail}" if detail else ""))


inst = kento.Instance.get(name)
print(f"# typed handle: {type(inst).__name__}  status={inst.status.value}")
check("get() -> running handle", inst.status is kento.Status.RUNNING, inst.status.value)

# M6 graceful stop of a cooperative guest: exercises stop.py graceful_only ->
# lxc-stop --nokill; the guest honors shutdown, so it stops (no StopTimeout).
t0 = time.time()
inst.stop()
check("stop() graceful -> returns", True, f"{time.time()-t0:.1f}s")
inst.refresh()
check("status STOPPED after stop()", inst.status is kento.Status.STOPPED, inst.status.value)

# M5 start
inst.start()
inst.refresh()
check("start() -> RUNNING", inst.status is kento.Status.RUNNING, inst.status.value)

# M6 force stop -> immediate
inst.stop(force=True)
inst.refresh()
check("stop(force=True) -> STOPPED", inst.status is kento.Status.STOPPED, inst.status.value)

print(f"\n=== live M6/M5: {'all ok' if not _fail else str(_fail) + ' FAILED'} ===")
sys.exit(1 if _fail else 0)
