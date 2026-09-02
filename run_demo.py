"""
Live demo entry point for Phase 3: the 3 simulated ECUs and the Edge
Gateway running together against one shared virtual CAN bus, with the
gateway publishing validated telemetry to a real MQTT broker.

Why this lives here instead of extending simulation/run_simulation.py or
adding a "gateway-only" script under edge_gateway/: python-can's virtual
bus only shares frames *within one OS process* (confirmed in Phase 1), so
the ECUs and the gateway must run as threads in this one process to see
each other's traffic at all -- a gateway-only script would have no bus
traffic to listen to, and simulation/run_simulation.py deliberately stays
ECU-only so Phase 1's original demo still works standalone. The gateway
is still a logically separate component (its own package, its own
session_id, its own thread) -- this is a simulation-environment
constraint, not an architectural merging of the two.

Requires a reachable MQTT broker (see docker/docker-compose.yml, or run
`mosquitto` directly) -- this script does not start one for you.

Usage:
    python run_demo.py --duration 10 --mqtt-host localhost --mqtt-port 1883
"""

from __future__ import annotations

import argparse
import logging
import threading

from common.telemetry_schema import DEFAULT_VEHICLE_ID
from edge_gateway.gateway import EdgeGateway
from edge_gateway.logging_config import configure_logging
from edge_gateway.mqtt_publisher import MqttPublisher
from simulation.can_bus import get_bus, run_ecu
from simulation.ecus.battery_ecu import BatteryECU
from simulation.ecus.body_ecu import BodyECU
from simulation.ecus.powertrain_ecu import PowertrainECU

simulator_logger = logging.getLogger("simulation")


def main(duration_seconds: float = None, mqtt_host: str = "localhost", mqtt_port: int = 1883) -> None:
    configure_logging(verbose=False)

    # --- ECUs: intentionally small duplication of run_simulation.py's
    # startup wiring (not its tick()/business logic) rather than modifying
    # Phase 1's own entry point, which stays ECU-only on purpose. ---
    ecu_session_id = "demo-ecu-session"  # only used for the ECUs' own in-memory bookkeeping/tests; never reaches the wire
    ecus = [
        PowertrainECU(ecu_session_id, DEFAULT_VEHICLE_ID),
        BatteryECU(ecu_session_id, DEFAULT_VEHICLE_ID),
        BodyECU(ecu_session_id, DEFAULT_VEHICLE_ID),
    ]
    ecu_buses = [get_bus() for _ in ecus]
    stop_event = threading.Event()
    ecu_threads = [
        threading.Thread(
            target=run_ecu, args=(ecu, bus, stop_event, ecu.TICK_INTERVAL_SECONDS, simulator_logger),
            daemon=True,
        )
        for ecu, bus in zip(ecus, ecu_buses)
    ]

    # --- Edge Gateway: its own bus handle, its own session_id ---
    gateway_bus = get_bus()
    publisher = MqttPublisher(host=mqtt_host, port=mqtt_port)
    publisher.connect()
    gateway = EdgeGateway(gateway_bus, publisher, vehicle_id=DEFAULT_VEHICLE_ID)
    gateway_thread = threading.Thread(target=gateway.run, args=(stop_event,), daemon=True)

    logging.getLogger("run_demo").info(
        "starting demo", extra={"gateway_session_id": gateway.session_id, "mqtt_host": mqtt_host, "mqtt_port": mqtt_port},
    )

    for thread in ecu_threads:
        thread.start()
    gateway_thread.start()

    try:
        if duration_seconds is not None:
            stop_event.wait(duration_seconds)
        else:
            stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        for thread in ecu_threads:
            thread.join(timeout=2)
        gateway_thread.join(timeout=2)
        for bus in ecu_buses:
            bus.shutdown()
        gateway_bus.shutdown()
        publisher.disconnect()
        logging.getLogger("run_demo").info(
            "demo stopped", extra={
                "processed": gateway.processed_count,
                "rejected": gateway.rejected_count,
                "publish_failures": gateway.publish_failure_count,
            },
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run the ECUs and the Edge Gateway together, publishing telemetry to a real MQTT broker."
    )
    parser.add_argument("--duration", type=float, default=None, help="Run for N seconds then stop")
    parser.add_argument("--mqtt-host", type=str, default="localhost")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    args = parser.parse_args()
    main(duration_seconds=args.duration, mqtt_host=args.mqtt_host, mqtt_port=args.mqtt_port)
