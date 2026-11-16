import argparse
import json
import sqlite3
from .engine import CohortEngine
from .policy import QueryPolicy


def main():
    parser = argparse.ArgumentParser(description="Policy-limited cohort analysis")
    parser.add_argument("--db", required=True)
    parser.add_argument("--policy")
    parser.add_argument("--tenant", default="demo")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("input")
    query = sub.add_parser("query")
    query.add_argument("input")
    query.add_argument("--principal", required=True)
    args = parser.parse_args()
    engine = None
    try:
        policy = QueryPolicy()
        if args.policy:
            with open(args.policy, encoding="utf-8") as stream: policy = QueryPolicy(**json.load(stream))
        engine = CohortEngine(args.db, policy)
        with open(args.input, encoding="utf-8") as stream: data = json.load(stream)
        if args.command == "ingest": result = {"revision": engine.ingest(args.tenant, data["subjects"], data.get("events", []))}
        else: result = engine.query(args.tenant, args.principal, data)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError, sqlite3.IntegrityError) as exc:
        parser.exit(2, f"error: {exc}\n")
    finally:
        if engine: engine.close()


if __name__ == "__main__":
    main()
