import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spend_desk")
    parser.add_argument(
        "command",
        choices=[
            "bootstrap",
            "data",
            "load",
            "build",
            "forecast",
            "anomaly",
            "recommend",
            "agent-eval",
            "ask",
            "eval",
            "deploy",
            "sync",
        ],
    )
    parser.add_argument("--target", choices=["duckdb", "snowflake"], default="duckdb")
    parser.add_argument("question", nargs="?", help="question for the ask command")
    parser.add_argument(
        "--snowflake-ml", action="store_true", help="train SNOWFLAKE.ML.FORECAST (uses credits)"
    )
    args = parser.parse_args(argv)

    if args.command == "eval":
        from spend_desk.evaluation import main as run_eval_all

        return run_eval_all()
    if args.command in ("sync", "deploy"):
        from spend_desk import deploy

        if args.command == "sync":
            deploy.sync()
        else:
            deploy.sync()
            deploy.deploy()
        return 0
    if args.command == "agent-eval":
        from spend_desk.agent.evaluate import run_eval

        print(run_eval(args.target)["summary"])
        return 0
    if args.command == "ask":
        from spend_desk.agent.agent import AnthropicAgent
        from spend_desk.agent.tools import Tools
        from spend_desk.config import load_config
        from spend_desk.models import open_target

        target = open_target(args.target)
        r = AnthropicAgent(Tools(target, load_config())).run(args.question)
        target.close()
        print(r.text)
        print()
        print(f"[provenance {'passed' if r.passed else 'FAILED'}, {len(r.calls)} tool calls]")
        return 0
    if args.command == "recommend":
        from spend_desk.recommend import main as run_recommend

        return run_recommend(args.target)
    if args.command == "anomaly":
        from spend_desk.anomaly import main as run_anomaly

        return run_anomaly(args.target, args.snowflake_ml)
    if args.command == "forecast":
        from spend_desk.forecast import main as run_forecast

        return run_forecast(args.target, args.snowflake_ml)

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
