"""Bound live latency without rewriting the robot's original clock or evidence."""

from .models import LiveObservation


def observation_elapsed(observation, window, now):
    if observation is None or window.quality.clock_status == "WALL_CLOCK_CHANGED":
        return None
    if (observation.stream_id, observation.sequence) != (window.stream_id, window.sequence):
        return None
    local_age = (observation.collector_sampled_at - window.window_end).total_seconds()
    transit_age = (now - observation.relay_observed_at).total_seconds()
    # Both comparisons use one machine's clock. Relay and central clocks must agree;
    # the robot clock may differ, but its monotonic anchor must remain stable.
    if not 0 <= local_age <= 3 or not -1 <= transit_age <= 5:
        return None
    elapsed = local_age + max(0, transit_age) + observation.round_trip_ms / 1000
    return elapsed if elapsed <= 5 else None


def live_metadata(observation, window, now):
    elapsed = observation_elapsed(observation, window, now)
    if elapsed is None:
        return None
    return {
        "observation": observation.model_dump(mode="json"),
        "elapsed_at_receive_seconds": elapsed,
    }


def snapshot_transport(payload, received_at, age):
    raw = payload.get("_live_transport") if payload else None
    if not isinstance(raw, dict) or age is None:
        return None
    try:
        observation = LiveObservation.model_validate(raw["observation"])
        from .models import TelemetryWindow

        window = TelemetryWindow.model_validate(
            {key: value for key, value in payload.items() if key != "_live_transport"}
        )
        base = observation_elapsed(observation, window, received_at)
        if base is None:
            return None
    except (ValueError, KeyError, TypeError):
        return None
    elapsed = base + age
    return {
        "basis": "relay_round_trip",
        "freshness_verified": elapsed <= 5,
        "elapsed_seconds": elapsed,
        "uncertainty_ms": observation.round_trip_ms,
        "clock_offset_seconds": (
            observation.relay_observed_at - observation.collector_sampled_at
        ).total_seconds(),
    }
