"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/main.py

Description:
    Script-style launcher for the ``qorotrainctl`` command line.

    Running ``python -m qorotrain.main <args>`` is equivalent to running
    ``qorotrainctl <args>``: it delegates to :func:`qorotrain.cli.main.main`
    and exits the interpreter with the returned exit code.

    Side Effects:
        The CLI runs and ``sys.exit`` is called **at import time**. Do not
        import this module from library code or documentation tooling; import
        :mod:`qorotrain.cli.main` instead. For that reason it is excluded from
        the generated API reference.

===============================================================================
"""
import sys

from .cli.main import main

sys.exit(main())
