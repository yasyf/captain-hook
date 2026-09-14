package hookd

import (
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
	"github.com/yasyf/daemonkit/artifact"
)

const (
	defaultRequestTimeout  = 30 * time.Second
	defaultShutdownTimeout = 60 * time.Second

	// packageLifecycleTimeout is one install or uninstall end to end: stopping
	// the installed app generation, draining a serving host through the grace
	// its own LaunchAgent promises it, and the sealed deployment verb after
	// them. A budget that cannot hold that drain fails on exactly the machine
	// the command exists to repair.
	packageLifecycleTimeout = 3 * time.Minute
)

// Main executes one capt-hookd client or host command and returns its exit code.
func Main(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprintln(stderr, "usage: capt-hookd version|serve|run|status|restart-workers|package-install|package-uninstall")
		return 2
	}
	if args[0] == "--root" || strings.HasPrefix(args[0], "--root=") {
		return runCommand(args, stdin, stdout, stderr)
	}
	switch args[0] {
	case "version":
		return versionCommand(args[1:], stdout, stderr)
	case "serve":
		return serveCommand(args[1:], stderr)
	case "run":
		return runCommand(args, stdin, stdout, stderr)
	case "status":
		return statusCommand(args[1:], stdout, stderr)
	case "restart-workers":
		return restartWorkersCommand(args[1:], stderr)
	case "package-install":
		return packageInstallCommand(args[1:], stderr)
	case "package-uninstall":
		return packageUninstallCommand(args[1:], stderr)
	default:
		fmt.Fprintf(stderr, "capt-hookd: unknown command %q\n", args[0])
		return 2
	}
}

func versionCommand(args []string, stdout, stderr io.Writer) int {
	if len(args) != 0 {
		fmt.Fprintln(stderr, "capt-hookd version: no arguments accepted")
		return 2
	}
	if err := json.NewEncoder(stdout).Encode(struct {
		Schema int    `json:"schema"`
		Build  string `json:"build"`
	}{Schema: wireproto.Schema, Build: Build}); err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	return 0
}

func serveCommand(args []string, stderr io.Writer) int {
	if len(args) != 0 {
		fmt.Fprintln(stderr, "capt-hookd serve: no arguments accepted")
		return 2
	}
	server, err := NewServer()
	if err == nil {
		err = server.Run(context.Background())
	}
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	return 0
}

func runCommand(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	request, timeout, code := eventRequest(args, stdin, stderr)
	if code != 0 {
		return code
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	client, err := NewClient()
	if err == nil {
		defer client.Close()
		err = client.EnsureCurrent(ctx)
	}
	var response wireproto.EventResponse
	if err == nil {
		response, err = client.Event(ctx, request)
	}
	if err != nil {
		fmt.Fprintln(stderr, eventFailure(err))
		return 1
	}
	_, stdoutErr := io.WriteString(stdout, response.Stdout)
	_, stderrErr := io.WriteString(stderr, response.Stderr)
	if err := errors.Join(stdoutErr, stderrErr); err != nil {
		fmt.Fprintf(stderr, "capt-hookd: write result: %v\n", err)
		return 1
	}
	if response.Exit < 0 || response.Exit > 255 {
		fmt.Fprintf(stderr, "capt-hookd: invalid worker exit code %d\n", response.Exit)
		return 1
	}
	return response.Exit
}

func eventRequest(args []string, stdin io.Reader, stderr io.Writer) (wireproto.EventRequest, time.Duration, int) {
	var event, root, cwd, python, build string
	var async bool
	timeout := durationFromEnvironment("CAPT_HOOK_CLIENT_TIMEOUT", defaultRequestTimeout)
	if len(args) > 1 && args[0] == "run" && args[1] == "--event" {
		flags := flag.NewFlagSet("run", flag.ContinueOnError)
		flags.SetOutput(stderr)
		flags.StringVar(&event, "event", "", "hook event")
		flags.StringVar(&root, "root", "", "project root")
		flags.StringVar(&cwd, "cwd", "", "request working directory")
		flags.StringVar(&python, "python", "", "exact Python executable")
		flags.StringVar(&build, "build", "", "exact Python product build")
		flags.BoolVar(&async, "async", false, "dispatch async hooks")
		flags.DurationVar(&timeout, "timeout", timeout, "request deadline")
		if err := flags.Parse(args[1:]); err != nil || flags.NArg() != 0 {
			return wireproto.EventRequest{}, 0, 2
		}
	} else {
		var ok bool
		if root, event, async, ok = parseHookRun(args); !ok {
			fmt.Fprintln(stderr, "usage: hook [--root ROOT] run EVENT [--async]")
			return wireproto.EventRequest{}, 0, 1
		}
	}
	if timeout <= 0 {
		fmt.Fprintf(stderr, "capt-hookd: request timeout %s must be positive\n", timeout)
		return wireproto.EventRequest{}, 0, 2
	}
	if cwd == "" {
		var err error
		if cwd, err = requestCWD(); err != nil {
			fmt.Fprintf(stderr, "capt-hookd: resolve cwd: %v\n", err)
			return wireproto.EventRequest{}, 0, 1
		}
	}
	root = cmp.Or(root, os.Getenv("CLAUDE_PROJECT_DIR"), os.Getenv("FACTORY_PROJECT_DIR"), cwd)
	build = cmp.Or(build, Build)
	if python == "" {
		var err error
		if python, err = productPython(); err != nil {
			fmt.Fprintf(stderr, "capt-hookd: resolve Python product %s: %v\n", Build, err)
			return wireproto.EventRequest{}, 0, 1
		}
	}
	payload, err := io.ReadAll(io.LimitReader(stdin, wireproto.MaxEventInput+1))
	if err != nil {
		fmt.Fprintf(stderr, "capt-hookd: read event: %v\n", err)
		return wireproto.EventRequest{}, 0, 1
	}
	if len(payload) > wireproto.MaxEventInput {
		fmt.Fprintf(stderr, "capt-hookd: event input exceeds %d bytes\n", wireproto.MaxEventInput)
		return wireproto.EventRequest{}, 0, 1
	}
	request := wireproto.EventRequest{
		Schema: wireproto.Schema, Event: event, Async: async, Root: root, CWD: cwd,
		Env: requestEnvironment(os.Environ()), PayloadRaw: string(payload),
		Python: python, Build: build, ClientPID: os.Getpid(), ClientPPID: os.Getppid(),
	}
	if err := request.Validate(); err != nil {
		fmt.Fprintln(stderr, err)
		return wireproto.EventRequest{}, 0, 2
	}
	if request.Build != Build {
		fmt.Fprintf(stderr, "capt-hookd: Python build %q does not match signed host build %q\n", request.Build, Build)
		return wireproto.EventRequest{}, 0, 1
	}
	return request, timeout, 0
}

func parseHookRun(args []string) (root, event string, async, ok bool) {
	index := 0
	if len(args) >= 2 && args[0] == "--root" {
		root, index = args[1], 2
	} else if len(args) > 0 && strings.HasPrefix(args[0], "--root=") {
		root, index = strings.TrimPrefix(args[0], "--root="), 1
	}
	tail := args[index:]
	if len(tail) != 2 && len(tail) != 3 || tail[0] != "run" || tail[1] == "" || strings.HasPrefix(tail[1], "-") {
		return "", "", false, false
	}
	if len(tail) == 3 && tail[2] != "--async" {
		return "", "", false, false
	}
	return root, tail[1], len(tail) == 3, true
}

// requestCWD answers what Python's os.getcwd does. os.Getwd prefers PWD's
// symlinked spelling, and on darwin syscall.Getwd still names a directory that
// is gone, even one recreated at that path, where libc's getcwd fails ENOENT;
// the identity check restores that failure. PWD is the fallback only then,
// since "/" as a root would walk the whole machine.
func requestCWD() (string, error) {
	cwd, err := syscall.Getwd()
	if err == nil {
		err = isWorkingDirectory(cwd)
	}
	if errors.Is(err, fs.ErrNotExist) {
		return cmp.Or(os.Getenv("PWD"), "/"), nil
	}
	return cwd, err
}

func isWorkingDirectory(path string) error {
	named, err := os.Stat(path)
	if err != nil {
		return err
	}
	here, err := os.Stat(".")
	if err != nil {
		return err
	}
	if !os.SameFile(named, here) {
		return fs.ErrNotExist
	}
	return nil
}

func productPython() (string, error) {
	store, err := artifact.DefaultStore()
	if err != nil {
		return "", err
	}
	entrypoint, err := store.Resolve(context.Background(), &artifact.Descriptor{
		Schema: 1, Name: "capt-hook", Kind: artifact.PythonTool,
		Version: artifact.VersionSource{Static: Build},
		Tool:    &artifact.ToolSpec{Dist: "capt-hook", Entrypoint: "hook"},
	})
	if err != nil {
		return "", err
	}
	return filepath.Join(filepath.Dir(entrypoint), "python"), nil
}

func statusCommand(args []string, stdout, stderr io.Writer) int {
	if len(args) != 0 {
		return 2
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	client, err := NewClient()
	if err == nil {
		defer client.Close()
	}
	var status statusResponse
	if err == nil {
		status, err = client.Status(ctx)
	}
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	encoder := json.NewEncoder(stdout)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(status); err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	return 0
}

func restartWorkersCommand(args []string, stderr io.Writer) int {
	if len(args) != 0 {
		return 2
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	client, err := NewClient()
	if err == nil {
		defer client.Close()
		err = client.RestartWorkers(ctx)
	}
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	return 0
}

func packageInstallCommand(args []string, stderr io.Writer) int {
	if len(args) != 0 {
		return 2
	}
	ctx, cancel := context.WithTimeout(context.Background(), packageLifecycleTimeout)
	defer cancel()
	if err := applyPackagedApplication(ctx); err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	return 0
}

func packageUninstallCommand(args []string, stderr io.Writer) int {
	if len(args) != 0 {
		return 2
	}
	ctx, cancel := context.WithTimeout(context.Background(), packageLifecycleTimeout)
	defer cancel()
	if err := uninstallPackagedApplication(ctx); err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	return 0
}

func requestEnvironment(environ []string) map[string]string {
	result := make(map[string]string)
	for _, item := range environ {
		name, value, ok := strings.Cut(item, "=")
		if !ok {
			continue
		}
		if name == "XDG_CACHE_HOME" || strings.HasPrefix(name, "CAPT_HOOK_") ||
			strings.HasPrefix(name, "CAPTAIN_HOOK_") || strings.HasPrefix(name, "HOOKS_") ||
			strings.HasPrefix(name, "CLAUDE_") || strings.HasPrefix(name, "FACTORY_") {
			result[name] = value
		}
	}
	return result
}

func durationFromEnvironment(name string, fallback time.Duration) time.Duration {
	if raw := os.Getenv(name); raw != "" {
		if seconds, err := strconv.ParseFloat(raw, 64); err == nil {
			return time.Duration(seconds * float64(time.Second))
		}
	}
	return fallback
}
