"""Allowlisted, context-local stage timing. No queries, headers or credentials."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import time

STAGES = frozenset({
    "search_total", "request_wait", "parse", "date_filter_dedup",
    "evidence_write", "evidence_verify", "checkpoint_write", "status_build",
    "pacing_sleep", "rate_limit_sleep", "backoff_sleep", "export",
    "json_serialize", "checkpoint_merge", "run_total",
})
_active = ContextVar("x_research_timings", default=None)


class StageTimings:
    def __init__(self, clock=time.perf_counter):
        self.clock = clock
        self.stages = {}
        self._stack = ContextVar("x_timing_stack", default=())

    @contextmanager
    def measure(self, stage):
        if stage not in STAGES:
            raise ValueError("Unknown timing stage")
        start = self.clock()
        frame = {"child_seconds": 0.0}
        parents = self._stack.get()
        token = self._stack.set(parents + (frame,))
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            seconds = max(0.0, self.clock() - start)
            self._stack.reset(token)
            if parents:
                parents[-1]["child_seconds"] += seconds
            row = self.stages.setdefault(stage, {"calls": 0, "errors": 0, "seconds": 0.0, "self_seconds": 0.0, "max_seconds": 0.0})
            row["calls"] += 1
            row["errors"] += failed
            row["seconds"] += seconds
            row["self_seconds"] += max(0.0, seconds - frame["child_seconds"])
            row["max_seconds"] = max(row["max_seconds"], seconds)

    def snapshot(self):
        return {
            "schema": "stage_timings_v1",
            "scope": "this_process_only_not_historical_runtime",
            "nested_stages_are_inclusive_do_not_sum": True,
            "self_seconds_excludes_nested_measured_stages": True,
            "stages": {name: {key: round(value, 6) if isinstance(value, float) else value
                              for key, value in row.items()} for name, row in sorted(self.stages.items())},
        }


def current_timings():
    return _active.get()


@contextmanager
def timing_scope(timings):
    token = _active.set(timings)
    try:
        yield timings
    finally:
        _active.reset(token)


@contextmanager
def measure(stage):
    timings = current_timings()
    if timings is None:
        yield
    else:
        with timings.measure(stage):
            yield


def timed(stage):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with measure(stage):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def measured_sleep(seconds, stage="pacing_sleep"):
    with measure(stage):
        time.sleep(seconds)
