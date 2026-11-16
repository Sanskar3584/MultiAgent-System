# Python Engineering Standards

All production Python must include type hints on public functions and a docstring that states behaviour, not implementation.

## Testing

Every new function ships with pytest unit tests in a test_*.py file next to it. Tests cover the happy path, at least two edge cases, and every rule stated in the relevant spec. Tests must be deterministic and must not touch the network or the filesystem outside a temporary directory.

## Dependencies

Prefer the standard library. Utility functions such as string normalisation should use unicodedata rather than a third-party package.

## Review

Code is merged only after automated tests pass and one reviewer approves. Reviewers check correctness against the spec first, readability second.
