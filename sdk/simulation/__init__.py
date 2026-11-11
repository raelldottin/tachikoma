"""Deterministic offline gameplay simulation (roadmap R6)."""

from sdk.simulation.game import (
    FAULT_KINDS,
    SCENARIO_DIR,
    Fault,
    ScenarioResult,
    SimulatedClock,
    SimulatedFaultError,
    SimulatedGame,
    load_scenario,
    run_scenario,
)

__all__ = ["FAULT_KINDS", "SCENARIO_DIR", "Fault", "ScenarioResult", "SimulatedClock", "SimulatedFaultError",
           "SimulatedGame", "load_scenario", "run_scenario"]
