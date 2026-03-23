# Contributing

Thanks for your interest in contributing to Kanary.

Pull requests are welcome.

If you find a bug, please open an issue first or include a clear explanation in your pull request.

If you want to propose a new helper class, helper rule, helper source, output plugin, or other extension, please open an issue first. That makes it easier to discuss API shape, naming, and scope before implementation.

## What to send where

- Bug reports: GitHub issues
- Feature requests and helper plugin proposals: GitHub issues
- Code changes and documentation fixes: GitHub pull requests

## Development notes

- Run tests before opening a pull request:

```bash
uv run python -m unittest discover -s tests
```

- If you add or change public behavior, please update the relevant documentation in `README.md` or `docs/`.
- If you add examples, keep them runnable and place them in `examples/`.

## Style

- Keep changes focused.
- Prefer simple, explicit APIs over heavy abstractions.
- Preserve the separation between `Source`, `Rule`, and `Output`.

## Questions

If you are unsure whether something belongs in core Kanary or should stay as a user-defined plugin or factory, open an issue and ask before implementing it.
