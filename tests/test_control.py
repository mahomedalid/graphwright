"""Model progress events, bounded waits, and iteration-limit validation."""

import asyncio

import pytest

from ddg.config import ModelSettings, RunLimits
from ddg.control import RunControl, StopRun


@pytest.mark.parametrize("iterations", [1, 3, 99])
def test_given_supported_iteration_limit_when_merging_then_exact_value_is_kept(iterations):
    assert RunLimits().merged({"max_iterations": iterations}).max_iterations == iterations


@pytest.mark.parametrize("iterations", [0, -1, 100, 1.5, True, "3.5"])
def test_given_invalid_iteration_limit_when_merging_then_error_is_explicit(iterations):
    with pytest.raises(ValueError):
        RunLimits().merged({"max_iterations": iterations})


def test_given_successful_model_call_when_recording_then_progress_and_usage_are_emitted():
    events = []
    control = RunControl(RunLimits(), ModelSettings(), lambda: False, on_event=events.append)

    async def request():
        async with control.model_call("discovery"):
            control.record_tokens(123, 45)

    asyncio.run(request())

    assert [event["type"] for event in events] == ["model_call_started", "model_call_completed"]
    assert events[0]["model_call"] == events[1]["model_call"] == 1
    assert events[1]["usage"]["input_tokens"] == 123
    assert events[1]["duration_seconds"] >= 0


def test_given_hung_model_when_time_budget_expires_then_wait_stops_and_failure_is_recorded():
    events = []
    control = RunControl(RunLimits(max_minutes=0.0005), ModelSettings(), lambda: False, on_event=events.append)

    async def request():
        async with control.model_call("topology_parsing"):
            await asyncio.sleep(10)

    with pytest.raises(StopRun) as error:
        asyncio.run(request())

    assert error.value.reason == "time_limit"
    assert control.elapsed_seconds < 1
    assert [event["type"] for event in events] == ["model_call_started", "model_call_failed"]
    assert events[-1]["reason"] == "time_limit"


def test_given_model_error_when_recording_then_exception_propagates_without_logging_response_contents():
    events = []
    control = RunControl(RunLimits(), ModelSettings(), lambda: False, on_event=events.append)

    async def request():
        async with control.model_call("discovery"):
            raise RuntimeError("sensitive model response")

    with pytest.raises(RuntimeError, match="sensitive"):
        asyncio.run(request())

    assert events[-1]["error_type"] == "RuntimeError"
    assert "sensitive" not in str(events)
