"""qorotrainctl: command line entry point.

Exit codes: 0 success, 1 validation errors, 2 usage or I/O errors.
"""
import argparse
import sys
from pathlib import Path

from ..config import (
    ConfigError,
    InitError,
    NotebookError,
    generate_experiment,
    load_experiment,
    validate_experiment,
)


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def cmd_init(args: argparse.Namespace) -> int:
    output = Path(args.output)
    if output.exists() and not args.force:
        _err(f"{output} already exists (use --force to overwrite)")
        return 2

    try:
        result = generate_experiment(args.notebook, output_dir=output.resolve().parent)
    except (InitError, NotebookError) as exc:
        _err(f"error: {exc}")
        return 2

    output.write_text(result.yaml, encoding="utf-8")
    print(f"wrote {output}")
    for name, guess in result.tuned.items():
        scale = ", log scale" if guess.log else ""
        print(f"  tuned  {name}: {guess.kind} [{guess.min:g}, {guess.max:g}]{scale}")
    for name in result.fixed:
        print(f"  fixed  {name}")
    if result.problems:
        print("\nthe parameters cell has lines init could not read (validate will report them):")
        for problem in result.problems:
            print(f"  {problem}")
    print("\nnext: set job.objective.metric, review the guessed ranges, then run `qorotrainctl validate`")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    path = Path(args.file)
    try:
        spec = load_experiment(path)
    except ConfigError as exc:
        _err(str(exc))
        return 1

    report = validate_experiment(spec, base_dir=path.resolve().parent, check_broker=not args.skip_broker)
    if report.errors or report.warnings:
        print(report.format())
    if report.ok:
        print(f"{path} is valid ({len(report.warnings)} warning(s))")
        return 0
    print(f"{path} has {len(report.errors)} error(s)")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qorotrainctl", description="Declarative LLM-driven training tuner")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="generate a starting experiment.yaml from a notebook")
    init.add_argument("notebook", help="notebook with a cell tagged 'parameters'")
    init.add_argument("-o", "--output", default="experiment.yaml")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    init.set_defaults(func=cmd_init)

    validate = sub.add_parser("validate", help="check experiment.yaml, the notebook and the broker")
    validate.add_argument("-f", "--file", required=True, help="path to experiment.yaml")
    validate.add_argument("--skip-broker", action="store_true", help="do not check broker reachability")
    validate.set_defaults(func=cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())