"""
    python -m dashboard.backend                       # http://127.0.0.1:8080, broker localhost:1883

Options (all optional): --host, --port, --mqtt-host, --mqtt-port,
--external-buffer PATH (read-only depth of another gateway's SQLite
buffer, e.g. run_demo.py's edge_gateway/data/buffer.db), --show-logs.
Environment fallbacks: DASHBOARD_HOST, DASHBOARD_PORT, MQTT_BROKER_HOST,
MQTT_BROKER_PORT.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

from dashboard.backend.server import Dashboard
from edge_gateway.logging_config import configure_logging


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Engineering dashboard for the telemetry POC (read/visualization layer).")
    parser.add_argument("--host", default=os.getenv("DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("DASHBOARD_PORT", "8080")))
    parser.add_argument("--mqtt-host", default=os.getenv("MQTT_BROKER_HOST", "localhost"))
    parser.add_argument("--mqtt-port", type=int, default=int(os.getenv("MQTT_BROKER_PORT", "1883")))
    parser.add_argument("--external-buffer", default=None,
                        help="also show the depth of this gateway buffer file (read-only)")
    parser.add_argument("--show-logs", action="store_true", help="also print gateway JSON logs to stderr")
    args = parser.parse_args(argv)

    try:
        dashboard = Dashboard(host=args.host, port=args.port, mqtt_host=args.mqtt_host, mqtt_port=args.mqtt_port,
                              external_buffer=args.external_buffer)
    except OSError as exc:
        print(f"Cannot listen on {args.host}:{args.port} ({exc}). Is another dashboard already running? "
              "Stop it or pass --port.", file=sys.stderr)
        return 2

    # Process-wide logging is configured only once the port is ours, so a
    # failed start leaves logging untouched. The dashboard shows gateway logs
    # itself (Activity panel); only echo them to the console when asked.
    configure_logging(verbose=False)
    logging.getLogger("edge_gateway").propagate = args.show_logs
    dashboard.start()
    print(f"Engineering dashboard: {dashboard.url}  (MQTT {args.mqtt_host}:{args.mqtt_port}; Ctrl+C to stop)", flush=True)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while not stop.wait(0.5):
        pass
    dashboard.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
