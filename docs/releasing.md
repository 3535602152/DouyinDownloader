# Release preparation

Version 0.4.0 targets Windows 10 22H2 / Windows 11 x64; source supports Python
3.12–3.14. The public repository contains only reviewed source history. Keep
release candidates as drafts until their validation and manual acceptance finish.

## Validate and build

1. Run the complete Python/Node checks from `CONTRIBUTING.md`, the dependency
   audit, release contracts and Gitleaks. Use a fresh clone and locked dependencies.
2. Run the manual `Build LGPL media component` workflow (it creates its
   maintainer-only staging draft as needed), or build from the fixed source/toolchain inputs. Verify
   it on Windows with `scripts/verify_media.py`, then install with
   `scripts/prepare_media.py`. Keep the corresponding-source directory.
3. Commit the final source. Run `powershell -File build_desktop_app.ps1` from the
   clean checkout. This creates an isolated Python 3.12 environment and verifies
   the packaged EXE using an isolated data directory. `-DevelopmentBuild` is for
   local inspection only; its outputs are not releasable.
4. Use the build interpreter to run `scripts/package_release.py --app
   build/main/DouyinDownloader --media build/media --output build/release`.
   It rejects dirty sources and includes licenses, dependency metadata and the
   corresponding media sources inside the Windows ZIP. Scan the extracted application payload as well.
5. Re-extract the application ZIP to a new folder, run `--self-test` with a new
   `DOUYIN_DOWNLOADER_DATA_DIR`, and verify UI startup and restart persistence.
   Phone QR confirmation and downloading content the tester may save require
   human participation; record those results separately from offline checks.
6. Only after checks pass, tag the tested commit `v0.4.0`, push that tag, and
   create a **draft** release. Upload only the Windows ZIP. Verify its uploaded
   SHA-256 digest and target commit. Retain validation records separately.
7. Run `Verify packaged Windows release` against that draft tag. It downloads
   the ZIP on a fresh GitHub-hosted Windows runner and checks its checksum, tag,
   self-test, real WebView2 window and restart persistence before sign-off.
   The native check runs `--self-test-ui`, then `--self-test-ui --expect-persistence`
   with an explicit isolated `DOUYIN_DOWNLOADER_DATA_DIR`. It uses the actual
   page-loaded event and rendered settings, without requiring a remote debugging
   port. Its JSON log is `desktop_ui_self_test.log`; do not include runtime logs
   in the release ZIP.

Automatic CI uses hosted runners without repository secrets. Media compilation
is a separate trusted manual workflow. Self-hosted workflows remain manual.
All Actions use pinned commits and explicit token permissions. If Actions
artifact storage is unavailable, retain local evidence and stage build outputs
only in a maintainer-only draft release; do not silently omit required checks.

## Privacy gates

Before replacing history, back up the complete Git directory, all refs/stashes,
remote refs, PR metadata and old artifacts outside the source repository. Verify
restoration and hashes. Push only the reviewed new root with an explicit
`--force-with-lease=refs/heads/main:<expected-old-sha>`; concurrent changes must
stop the replacement. Never mirror-push local checkpoint refs or old backups.

Re-clone after replacement. Old working copies must not merge/push their history
back. Existing account data and downloaded media stay outside Git and are not
part of the migration.

Old PR refs and cached SHA URLs may remain accessible after history replacement.
Prepare a GitHub Support request with the affected PRs/commits and request cache
and reference removal. Record GitHub's response; do not describe these copies
as removed until verified. Never import PR refs, old Git metadata, Actions logs,
or historical backup attachments when starting a separate public repository.
`scripts/configure_repository.py --apply` configures the public repository's
available protections and records exact API outcomes without changing visibility
or buying a subscription. `--expected-visibility private` is only for explicitly
staged private preparation; the script verifies the exact repository name first.
