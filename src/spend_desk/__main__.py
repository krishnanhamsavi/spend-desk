import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spend_desk")
    parser.add_argument("command", choices=["bootstrap", "data", "load", "build", "eval", "deploy"])
    parser.add_argument("--target", choices=["duckdb", "snowflake"], default="duckdb")
    args = parser.parse_args(argv)

    if args.command == "build":
        from spend_desk.models import main as build_models

        return build_models(args.target)

    if args.command == "bootstrap":
        from spend_desk.sf import run_bootstrap

        run_bootstrap()
        return 0
    if args.command == "data":
        from spend_desk.generate.build import main as build_data

        build_data()
        return 0
    if args.command == "load":
        from spend_desk.sf import load_raw

        load_raw()
        return 0

    print(f"'{args.command}' is not built yet.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
