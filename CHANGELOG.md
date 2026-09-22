# Changelog

keep a changelog format, semver. before 1.0 a breaking change bumps the minor.

## [Unreleased]

## [0.1.0] - 2026-09-21

### Added
- workflows with steps, compensation, retries, optional steps, timers, signals
- compensation written into the generated canon, runs in reverse
- resumption by journal replay, no separate state store
- retries unrolled rather than looped
- runtime handlers, signal delivery, execution trace
