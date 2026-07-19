# helper scripts

Local build and smoke-test helpers for the Captain Hook app. None of these run
in CI. The release lane builds and signs the app itself.

| Script | What it does |
|---|---|
| `make-appicon.sh` | Resizes `docs/assets/logo.png` into the `AppIcon.appiconset` PNGs (sizes 16 to 1024) with `sips`. Run it before generating the project so the asset catalog has icons. |
| `build-local.sh [version]` | Generates the project, builds Release, signs with your Developer ID, verifies the embedded bridge identity and hardened runtime, exercises signed ping/notify sessions, then installs to `/Applications` and relaunches. |
| `notify-test.sh [ping\|notify]` | Invokes the installed signed bridge. `ping` returns the version; `notify` posts a sample `pr_open` banner. |

## Build without installing

```sh
./gen-version-xcconfig.sh 1.0.0
xcodegen generate
xcodebuild -scheme CaptainHook -destination 'platform=macOS' build CODE_SIGNING_ALLOWED=NO
xcodebuild test -scheme CaptainHook -destination 'platform=macOS' CODE_SIGNING_ALLOWED=NO
```

`build-local.sh` installs to `/Applications` and touches Login Items. Run it only
on a machine where you want the real helper installed.
