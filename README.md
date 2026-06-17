# personal-tools

A small monorepo of personal developer tools. Each tool lives in its own
top-level directory with its own docs; this README is the index.

## Tools

| Tool | What it does |
| --- | --- |
| [`session-coach/`](session-coach/) | Analyzes past Claude Code session transcripts to surface prompting weaknesses and knowledge gaps, then emits a prompting-journal entry, behavior-shaping memory entries, and an Anki flashcard deck. |

## Development

The tools are Python 3.12. Runtime dependencies are pinned per tool (e.g.
`session-coach/anki/requirements.txt`); test/dev dependencies are in
[`requirements-dev.txt`](requirements-dev.txt).

```sh
make venv    # create the dev virtualenv (genanki + pytest)
make test    # run the full test suite
make clean   # remove __pycache__ / *.pyc
```

`make venv` builds the virtualenv at `session-coach/anki/.venv` (gitignored) and
`make test` runs `pytest` through it. The Anki builder needs `genanki`, which is
why tests run inside that venv rather than the system interpreter.

To run a single tool's tests directly:

```sh
session-coach/anki/.venv/bin/python -m pytest session-coach/anki
```

## Layout

```
personal-tools/
├── Makefile              # venv / test / clean
├── pyproject.toml        # pytest config (discovers all tools' tests)
├── requirements-dev.txt  # test/dev deps for the whole repo
└── session-coach/        # first tool — see its ARCHITECTURE.md
```
