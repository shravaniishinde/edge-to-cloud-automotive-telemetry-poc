"""
Entry point that runs all 3 simulated ECUs together on the virtual CAN bus.

This is a manual/demo script, not something pytest calls -- the test suite
exercises each ECU's tick()/encode()/decode() logic directly plus a short,
bounded bus exchange instead of running this open-ended loop.

Usage:
    python -m simulation.run_simulation --duration 5 --seed 42
    python -m simulation.run_simulation --duration 5 --seed 42 --verbose

Each ECU runs in its own thread, on its own bus handle, so it can publish
at its own rate (10 Hz / 2 Hz / 1 Hz) independently. Every ECU gets its own
`can.Bus()` object (rather than sharing one) even though they're all on the
same virtual channel -- this matches the pattern used in the test suite and
avoids any question about whether one Bus object is safe to call from
multiple threads at once.
"""

from __future__ import annotations

import argparse
import logging
import threading
import uuid

from common.telemetry_schema import DEFAULT_VEHICLE_ID
from simulation.can_bus import get_bus, run_ecu
from simulation.ecus.battery_ecu import BatteryECU
from simulation.ecus.body_ecu import BodyECU
from simulation.ecus.powertrain_ecu import PowertrainECU

logger = logging.getLogger("simulation")


def main(duration_seconds: float = None, seed: int = None, verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    session_id = str(uuid.uuid4())
    logger.info(
        "Starting simulation run session_id=%s vehicle_id=%s", session_id, DEFAULT_VEHICLE_ID
    )

    ecus = [
        PowertrainECU(session_id, DEFAULT_VEHICLE_ID, rng_seed=seed),
        BatteryECU(session_id, DEFAULT_VEHICLE_ID, rng_seed=seed),
        BodyECU(session_id, DEFAULT_VEHICLE_ID, rng_seed=seed),
    ]
    buses = [get_bus() for _ in ecus]
    stop_event = threading.Event()

    threads = [
        threading.Thread(
            target=run_ecu,
            args=(ecu, bus, stop_event, ecu.TICK_INTERVAL_SECONDS, logger),
            daemon=True,
        )
        for ecu, bus in zip(ecus, buses)
    ]
    for thread in threads:
        thread.start()

    try:
        if duration_seconds is not None:
            stop_event.wait(duration_seconds)
        else:
            stop_event.wait()  # runs until interrupted (Ctrl+C)
    except KeyboardInterrupt:
        logger.info("Interrupted, stopping simulation")
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=2)
        for bus in buses:
            bus.shutdown()
        logger.info("Simulation stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run the simulated 3-ECU vehicle network on a virtual CAN bus."
    )
    parser.add_argument(
        "--duration", type=float, default=None,
        help="Run for N seconds then stop (default: run until Ctrl+C)",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="Random seed, for reproducible telemetry across runs",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Log every CAN frame sent, not just start/stop",
    )
    args = parser.parse_args()
    main(duration_seconds=args.duration, seed=args.seed, verbose=args.verbose)
