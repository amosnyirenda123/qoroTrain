"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/__main__.py

Description:
    Module entry point: ``python -m qorotrain <args>`` is the same command as
    ``qorotrainctl <args>``.

    It delegates to :func:`qorotrain.cli.main.main`, the function the
    ``qorotrainctl`` console script calls, and exits with its return code. All
    argument parsing, exit codes and Ctrl-C handling (exit code 130) live in
    the CLI; nothing is duplicated here. This form is useful where the console
    script is not on ``PATH`` and for running the CLI with a specific
    interpreter (``.venv/bin/python -m qorotrain``).

    The ``__name__`` guard keeps the module importable without side effects
    (unlike ``qorotrain.main``, which runs the CLI when imported).

===============================================================================
"""
import sys

from .cli.main import main

if __name__ == "__main__":
    sys.exit(main())
