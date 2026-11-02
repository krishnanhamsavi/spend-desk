import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spend_desk")
    parser.add_argument("command", choices=["data", "eval", "deploy"])
    args = parser.parse_args(argv)
    print(f"'{args.command}' is not built yet.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
