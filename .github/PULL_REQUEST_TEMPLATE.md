<!-- Describe the change and why it is worth making. What broke, or what could
     not be expressed before? -->

## What this changes

## Checks

- [ ] `pytest -q` passes
- [ ] `ruff check` and `ruff format --check` pass
- [ ] `mypy` passes
- [ ] New behaviour has a test that fails without the change
- [ ] A new check type also has a test for its *misconfiguration* error
      (malformed rules must fail at load time, never at check time)
- [ ] New user-visible messages say what was wrong **and** what to do instead
