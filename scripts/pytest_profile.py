"""Read-only pytest timing hooks and bounded frame sampler; no test behavior patches."""

import collections
import json
import os
import resource
import sys
import threading
import time
from pathlib import Path

import pytest

START = time.perf_counter()
REPORTS = []
FIXTURES = collections.defaultdict(lambda: [0, 0.0])
SAMPLES = collections.Counter()
STOP = threading.Event()
COLLECTION = 0.0
DESELECTED = []
COLLECTION_ERRORS = []
SAMPLER = None


def sample():
    me = threading.get_ident()
    while not STOP.wait(0.05):
        for ident, frame in sys._current_frames().items():
            if ident == me:
                continue
            labels = []
            while frame is not None:
                path = frame.f_code.co_filename
                if "/terminal_mcp/" in path or "/tests/" in path:
                    labels.append(
                        path.split("/terminal_mcp/")[-1]
                        if "/terminal_mcp/" in path
                        else "tests/" + path.split("/tests/")[-1]
                    )
                    labels[-1] += ":" + frame.f_code.co_name + ":" + str(frame.f_lineno)
                elif any(
                    s in path
                    for s in (
                        "subprocess.py",
                        "selectors.py",
                        "argon2",
                        "aiosqlite",
                        "sqlite3",
                        "cryptography",
                        "tiktoken",
                    )
                ):
                    labels.append(
                        Path(path).name + ":" + frame.f_code.co_name + ":" + str(frame.f_lineno)
                    )
                frame = frame.f_back
            if labels:
                SAMPLES[tuple(labels[:5])] += 1


@pytest.hookimpl(hookwrapper=True)
def pytest_collection(session):
    global COLLECTION
    before = time.perf_counter()
    yield
    COLLECTION = time.perf_counter() - before


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    before = time.perf_counter()
    yield
    key = fixturedef.argname + "@" + str(fixturedef.baseid) + ":" + fixturedef.scope
    FIXTURES[key][0] += 1
    FIXTURES[key][1] += time.perf_counter() - before


def pytest_sessionstart(session):
    global SAMPLER
    if os.environ.get("TMCP_PROFILE_SAMPLE") == "1":
        SAMPLER = threading.Thread(target=sample, name="pytest-readonly-sampler", daemon=True)
        SAMPLER.start()


def pytest_deselected(items):
    DESELECTED.extend(item.nodeid for item in items)


def pytest_collectreport(report):
    if report.failed:
        COLLECTION_ERRORS.append(report.nodeid)


def pytest_runtest_logreport(report):
    REPORTS.append(
        {
            "nodeid": report.nodeid,
            "phase": report.when,
            "seconds": report.duration,
            "outcome": report.outcome,
            "wasxfail": bool(getattr(report, "wasxfail", False)),
        }
    )


def pytest_sessionfinish(session, exitstatus):
    STOP.set()
    if SAMPLER is not None:
        SAMPLER.join(timeout=1)
    phases = {
        p: sum(r["seconds"] for r in REPORTS if r["phase"] == p)
        for p in ("setup", "call", "teardown")
    }
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    result = {
        "schema": 1,
        "exit_code": int(exitstatus),
        "elapsed_seconds": time.perf_counter() - START,
        "collection_seconds": COLLECTION,
        "phase_seconds": phases,
        "collected": session.testscollected,
        "deselected": DESELECTED,
        "collection_errors": COLLECTION_ERRORS,
        "reports": REPORTS,
        "fixture_setup_inclusive": dict(FIXTURES),
        "samples": [{"stack": list(k), "samples": v} for k, v in SAMPLES.most_common(120)],
        "resource": {
            "user_cpu": own.ru_utime,
            "system_cpu": own.ru_stime,
            "max_rss_kib": own.ru_maxrss,
            "children_user_cpu": children.ru_utime,
            "children_system_cpu": children.ru_stime,
            "children_max_rss_kib": children.ru_maxrss,
        },
        "load_end": os.getloadavg(),
    }
    Path(os.environ["TMCP_PROFILE_PATH"]).write_text(json.dumps(result, indent=2) + "\n")
