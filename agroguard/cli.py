import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone

from .config import load_settings


def main():
    parser = argparse.ArgumentParser(prog="agroguard")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--demo", action="store_true")
    bench = commands.add_parser("benchmark")
    bench.add_argument("--seed", type=int, default=42)
    bench.add_argument("--size", type=int, default=300)
    bench.add_argument("--output", default="artifacts/benchmark")
    bench.add_argument("--csv", help="Clean historical CSV; select a single sensor")
    bench.add_argument("--mapping", help="JSON CSV mapping/defaults")
    bench.add_argument("--sensor-id", help="Sensor to benchmark from historical CSV")
    quality = commands.add_parser(
        "evaluate", help="Compare current and previous rules on controlled scenarios"
    )
    quality.add_argument("--seeds", default="11,29,47,83,101")
    quality.add_argument("--output", default="artifacts/quality")
    sim = commands.add_parser("simulate")
    sim.add_argument("--url", default="http://127.0.0.1:8000")
    sim.add_argument("--steps", type=int, default=120)
    sim.add_argument("--interval", type=float, default=1)
    sim.add_argument("--scenario", action="store_true")
    csv = commands.add_parser("import-csv")
    csv.add_argument("path")
    csv.add_argument("--mapping", required=True, help="JSON file with mapping and defaults")
    csv.add_argument("--url", default="http://127.0.0.1:8000")
    broker = commands.add_parser("mqtt")
    broker.add_argument("--host", default="localhost")
    broker.add_argument("--port", type=int, default=1883)
    broker.add_argument("--topic", default="agroguard/v1/measurements")
    broker.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    from .api import JsonFormatter

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.getLogger("agroguard").addHandler(handler)
    logging.getLogger("agroguard").setLevel(logging.INFO)
    settings = load_settings()
    if args.command == "serve":
        import uvicorn
        from .api import create_app

        if args.host not in ("localhost", "127.0.0.1", "::1") and not os.getenv("API_KEY"):
            parser.error("Set API_KEY before binding a non-loopback address")
        uvicorn.run(
            create_app(settings=settings, auto_demo=args.demo or os.getenv("AGRO_DEMO") == "1"),
            host=args.host,
            port=args.port,
        )
    elif args.command == "benchmark":
        from .benchmark import run_benchmark

        if not 200 <= args.size <= 1500 or args.seed < 0:
            parser.error("size must be 200..1500 and seed non-negative")
        clean = None
        if args.csv:
            from pathlib import Path
            from .sources import CSVSource

            if not args.mapping or not args.sensor_id:
                parser.error("Historical benchmark requires --mapping and --sensor-id")
            options = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
            clean = [m for m in CSVSource(args.csv, **options).read() if m.sensor_id == args.sensor_id]
        report = run_benchmark(settings, args.seed, args.size, args.output, clean_measurements=clean)
        print(
            json.dumps(
                {"id": report["id"], "output": args.output, "detectors": report["detectors"]}, indent=2
            )
        )
    elif args.command == "evaluate":
        from .quality_suite import compare_quality

        try:
            seeds = [int(s.strip()) for s in args.seeds.split(",")]
            report = compare_quality(settings, seeds, args.output)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps({"output": args.output, "totals": report["totals"]}, indent=2))
    elif args.command == "mqtt":
        from .sources import mqtt_bridge

        mqtt_bridge(args.host, args.port, args.topic, args.url)
    else:
        from .sources import CSVSource, HTTPTransport

        transport = HTTPTransport(args.url)
        try:
            if args.command == "import-csv":
                from pathlib import Path

                options = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
                # Validate whole file before sending; delivery atomic per batch, not per file.
                rows = list(CSVSource(args.path, **options).read())
                for i in range(0, len(rows), settings.max_batch):
                    transport.send(rows[i : i + settings.max_batch])
                print(f"Imported {len(rows)} observations")
            else:
                from .simulator import Simulator

                sim = Simulator(settings, start=datetime.now(timezone.utc), namespace="network")
                ids = list(sim.channels)
                for step in range(args.steps):
                    if args.scenario and step in (30, 45, 60, 90):
                        kind = {30: "spike", 45: "missing", 60: "stuck", 90: "drift"}[step]
                        sim.inject(ids[0], kind, 1 if kind == "spike" else 12, 25)
                    rows = sim.step(60)
                    if rows:
                        transport.send([m for m, truth in rows])
                    # Keep ground truth separate from measurement payloads.
                    from pathlib import Path

                    Path("artifacts").mkdir(exist_ok=True)
                    with open("artifacts/network-truth.jsonl", "a", encoding="utf-8") as stream:
                        for m, truth in rows:
                            stream.write(json.dumps(truth) + "\n")
                    time.sleep(args.interval)
        except KeyboardInterrupt:
            pass
        finally:
            transport.close()


if __name__ == "__main__":
    main()
