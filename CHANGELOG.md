# Changelog

All notable changes to this project. This project adheres to semantic versioning.

## v0.2.2 — 2026-11-16

- Attach a CycloneDX SBOM to each release (covered by the signed checksums)

## v0.2.1 — 2026-11-15

- Sign release checksums with Sigstore (keyless) and attach the `.sigstore.json` bundle
- Add property/fuzz hardening tests for the AES-256-GCM + scrypt envelope

## v0.2.0 — 2026-11-14

- Add per-track streak and drift stats
- Add a completion history view
- Add CSV export of the orbit status
- Add snooze: defer a track's next due date
- Add iCal (.ics) export of each track's next due date
- Add a Copy id action to the Tracks tab
- Docs: point install at the live v0.1.0 release

## v0.1.0

- Initial public release.
