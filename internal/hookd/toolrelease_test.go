package hookd

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func fakeUV(t *testing.T, failures int) (uv, requests string) {
	t.Helper()
	dir := t.TempDir()
	requests = filepath.Join(dir, "requests")
	uv = filepath.Join(dir, "uv")
	script := fmt.Sprintf(`#!/bin/sh
cat >> %[1]q
echo "$@" >> %[1]q
if [ "$(grep -c '^pip compile' %[1]q)" -le %[2]d ]; then
  echo "No solution found when resolving dependencies" >&2
  exit 1
fi
`, requests, failures)
	if err := os.WriteFile(uv, []byte(script), 0o700); err != nil { //nolint:gosec // a fake uv must be executable
		t.Fatal(err)
	}
	return uv, requests
}

func TestAwaitResolvableHoldsUntilUVResolvesTheVersion(t *testing.T) {
	t.Parallel()
	uv, requests := fakeUV(t, 1)
	if err := awaitResolvable(t.Context(), uv, productDist, "12.88.0"); err != nil {
		t.Fatalf("awaitResolvable = %v, want nil once uv resolves 12.88.0", err)
	}
	got, err := os.ReadFile(requests)
	if err != nil {
		t.Fatal(err)
	}
	probe := "capt-hook==12.88.0\npip compile --no-deps --refresh-package capt-hook --quiet --no-header -\n"
	if string(got) != probe+probe {
		t.Fatalf("uv saw %q, want two resolution probes", got)
	}
}

func TestAwaitResolvableGivesUpAtItsDeadlineNamingTheVersion(t *testing.T) {
	t.Parallel()
	uv, _ := fakeUV(t, 9)
	ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
	defer cancel()
	err := awaitResolvable(ctx, uv, productDist, "12.88.0")
	if err == nil || !strings.Contains(err.Error(), "uv cannot resolve capt-hook==12.88.0: No solution found") {
		t.Fatalf("awaitResolvable = %v, want the unresolved version named", err)
	}
}

func TestAwaitResolvableFailsAtOnceWithoutUV(t *testing.T) {
	t.Parallel()
	ctx, cancel := context.WithTimeout(t.Context(), time.Minute)
	defer cancel()
	start := time.Now()
	err := awaitResolvable(ctx, filepath.Join(t.TempDir(), "uv"), productDist, "12.88.0")
	if err == nil || time.Since(start) > releasePollEvery {
		t.Fatalf("awaitResolvable without uv = %v after %v, want an immediate failure", err, time.Since(start))
	}
}
