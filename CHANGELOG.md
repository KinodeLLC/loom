# Changelog

Keep a Changelog format, SemVer. Pre-1.0: breaking changes bump the minor.

## [Unreleased]

## [0.1.0] - 2026-09-21

### Added
- Workflows with steps, compensation, bounded retries, optional steps,
  durable timers and signal waits.
- Compensation emitted explicitly into the generated Canon, so the unwinding
  for every failure point is visible before it runs and provably in reverse.
- Resumption by journal replay rather than a separate state store: completed
  steps return recorded results without being performed again.
- Retries unrolled rather than looped, keeping workflows total and the external
  call count visible in the source.
- Runtime handlers, signal delivery and an execution trace.
