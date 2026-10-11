## Summary

<!-- What does this change, and why? -->

## Checklist

- [ ] `make pre-commit` passes (ruff + mypy strict + tests, coverage ≥ 95%)
- [ ] If `usage_display.py` changed: ran `make bump PART=major|minor|patch` and described the change under the new `CHANGELOG.md` heading (CI fails otherwise; merging publishes the release)
- [ ] Verified behavior against Open WebUI (note the version tested)
