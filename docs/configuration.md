# Configuration reference

Every key of `experiment.yaml`, with its type, default and effect. Unknown keys
are rejected. `${NAME}` anywhere in a value is replaced by the environment
variable `NAME` when the file is loaded. Relative paths are relative to the
directory containing `experiment.yaml`.

Keys marked **Not used yet.** are validated but no code reads them.
**Label only.** keys appear only in messages.

```{note}
The tables below are generated from the schema in `qorotrain.config.models`
and the descriptions in `qorotrain.config.reference`. Do not edit
`docs/experiment-yaml-reference.md` by hand. Regenerate it with:

    python -m qorotrain.config.reference > docs/experiment-yaml-reference.md

`tests/test_reference.py` fails if the checked-in page is out of date.
```

```{include} experiment-yaml-reference.md
```
