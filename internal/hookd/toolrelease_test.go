package hookd

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	captainhook "github.com/yasyf/captain-hook"
)

func fakeUV(t *testing.T, failures int, supported string) (uv, requests string) {
	t.Helper()
	dir := t.TempDir()
	requests = filepath.Join(dir, "requests")
	uv = filepath.Join(dir, "uv")
	script := fmt.Sprintf(`#!/bin/sh
cat >> %[1]q
echo "$@" >> %[1]q
case " $* " in
*" --python-version %[3]s "*) ;;
*" --python-version "*)
  echo "No solution found when resolving dependencies: the requested Python version does not satisfy Python>=%[3]s" >&2
  exit 1 ;;
*)
  echo "No solution found when resolving dependencies: the current Python version (3.12.3) does not satisfy Python>=%[3]s" >&2
  exit 1 ;;
esac
if [ "$(grep -c '^pip compile' %[1]q)" -le %[2]d ]; then
  echo "No solution found when resolving dependencies" >&2
  exit 1
fi
`, requests, failures, supported)
	if err := os.WriteFile(uv, []byte(script), 0o700); err != nil { //nolint:gosec // a fake uv must be executable
		t.Fatal(err)
	}
	return uv, requests
}

func productFloor(t *testing.T) string {
	t.Helper()
	floor, err := pythonFloor(captainhook.Pyproject)
	if err != nil {
		t.Fatal(err)
	}
	return floor
}

func probe(version, python string) string {
	return "capt-hook==" + version + "\npip compile --no-deps --refresh-package capt-hook --python-version " + python +
		" --quiet --no-header -\n"
}

func TestAwaitResolvableHoldsUntilUVResolvesTheVersion(t *testing.T) {
	t.Parallel()
	floor := productFloor(t)
	uv, requests := fakeUV(t, 1, floor)
	ctx, cancel := context.WithTimeout(t.Context(), 2*releasePollEvery)
	defer cancel()
	if err := awaitResolvable(ctx, uv, productDist, "12.88.0", floor); err != nil {
		t.Fatalf("awaitResolvable = %v, want nil once uv resolves 12.88.0", err)
	}
	got, err := os.ReadFile(requests)
	if err != nil {
		t.Fatal(err)
	}
	if want := probe("12.88.0", floor); string(got) != want+want {
		t.Fatalf("uv saw %q, want two resolution probes", got)
	}
}

func TestAwaitResolvableResolvesForTheProductPythonOnAnOlderHost(t *testing.T) {
	t.Parallel()
	floor := productFloor(t)
	uv, requests := fakeUV(t, 0, floor)
	ctx, cancel := context.WithTimeout(t.Context(), time.Second)
	defer cancel()
	if err := awaitResolvable(ctx, uv, productDist, "12.88.9", floor); err != nil {
		t.Fatalf("awaitResolvable = %v, want the first probe to resolve for Python %s", err, floor)
	}
	got, err := os.ReadFile(requests)
	if err != nil {
		t.Fatal(err)
	}
	if want := probe("12.88.9", floor); string(got) != want {
		t.Fatalf("uv saw %q, want one probe for Python %s", got, floor)
	}
	ctx, cancel = context.WithTimeout(t.Context(), time.Second)
	defer cancel()
	err = awaitResolvable(ctx, uv, productDist, "12.88.9", "3.12")
	if err == nil || !strings.Contains(err.Error(), "uv cannot resolve capt-hook==12.88.9: No solution found") ||
		!strings.Contains(err.Error(), "does not satisfy Python>="+floor) {
		t.Fatalf("awaitResolvable for the host's Python = %v, want the unsatisfied requires-python named", err)
	}
}

func TestAwaitResolvableGivesUpAtItsDeadlineNamingTheVersion(t *testing.T) {
	t.Parallel()
	floor := productFloor(t)
	uv, _ := fakeUV(t, 9, floor)
	ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
	defer cancel()
	err := awaitResolvable(ctx, uv, productDist, "12.88.0", floor)
	if err == nil || !strings.Contains(err.Error(), "uv cannot resolve capt-hook==12.88.0: No solution found") {
		t.Fatalf("awaitResolvable = %v, want the unresolved version named", err)
	}
}

func TestAwaitResolvableReturnsNonResolutionExitWithItsDiagnostic(t *testing.T) {
	t.Parallel()
	for _, code := range []int{2, 7} {
		t.Run(fmt.Sprintf("exit%d", code), func(t *testing.T) {
			t.Parallel()
			floor := productFloor(t)
			dir := t.TempDir()
			uv := filepath.Join(dir, "uv")
			requests := filepath.Join(dir, "requests")
			const diagnostic = "publication probe cannot continue"
			script := fmt.Sprintf("#!/bin/sh\ncat >> %q\necho \"$@\" >> %q\necho %q >&2\nexit %d\n",
				requests, requests, diagnostic, code)
			if err := os.WriteFile(uv, []byte(script), 0o700); err != nil { //nolint:gosec // a fake uv must be executable
				t.Fatal(err)
			}
			ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
			defer cancel()
			err := awaitResolvable(ctx, uv, productDist, "12.88.0", floor)
			var exit *exec.ExitError
			if !errors.As(err, &exit) || exit.ExitCode() != code {
				t.Fatalf("awaitResolvable = %v, want wrapped uv exit %d", err, code)
			}
			if errors.Is(err, context.DeadlineExceeded) || ctx.Err() != nil {
				t.Fatalf("awaitResolvable = %v, context = %v, want the uv failure before its deadline", err, ctx.Err())
			}
			if !strings.Contains(err.Error(), "capt-hook==12.88.0: "+diagnostic) {
				t.Fatalf("awaitResolvable = %v, want the version and uv diagnostic", err)
			}
			got, err := os.ReadFile(requests)
			if err != nil {
				t.Fatal(err)
			}
			if want := probe("12.88.0", floor); string(got) != want {
				t.Fatalf("uv saw %q, want one exact resolution probe %q", got, want)
			}
		})
	}
}

func TestAwaitResolvableHonorsCancellationBeforeStartingUV(t *testing.T) {
	t.Parallel()
	uv, requests := fakeUV(t, 0, productFloor(t))
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if err := awaitResolvable(ctx, uv, productDist, "12.88.0", productFloor(t)); !errors.Is(err, context.Canceled) {
		t.Fatalf("awaitResolvable = %v, want context cancellation", err)
	}
	if _, err := os.Stat(requests); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("uv requests = %v, want no invocation after cancellation", err)
	}
}

func TestAwaitResolvableFailsAtOnceWithoutUV(t *testing.T) {
	t.Parallel()
	ctx, cancel := context.WithTimeout(t.Context(), time.Minute)
	defer cancel()
	start := time.Now()
	err := awaitResolvable(ctx, filepath.Join(t.TempDir(), "uv"), productDist, "12.88.0", productFloor(t))
	if err == nil || time.Since(start) > releasePollEvery {
		t.Fatalf("awaitResolvable without uv = %v after %v, want an immediate failure", err, time.Since(start))
	}
}

func TestPythonFloorReadsTheRequiresPythonLowerBound(t *testing.T) {
	t.Parallel()
	for pyproject, want := range map[string]string{
		"[project]\nname = \"capt-hook\"\nrequires-python = \">=3.13\"\n": "3.13",
		"[project]\nrequires-python = \">=3.13.2, <3.16\"\n":              "3.13.2",
	} {
		if got, err := pythonFloor(pyproject); err != nil || got != want {
			t.Errorf("pythonFloor(%q) = %q, %v, want %q", pyproject, got, err, want)
		}
	}
	for pyproject, reason := range map[string]string{
		"[project]\nrequires-python = \"==3.13.*\"\n":               "names no lower bound",
		"[project]\n# requires-python = \">=3.13\"\nname = \"x\"\n": "declares no requires-python",
	} {
		if _, err := pythonFloor(pyproject); err == nil || !strings.Contains(err.Error(), reason) {
			t.Errorf("pythonFloor(%q) = %v, want %q", pyproject, err, reason)
		}
	}
	if floor := productFloor(t); !strings.HasPrefix(floor, "3.") {
		t.Fatalf("the product's Python floor %q is not a Python 3 version", floor)
	}
}
