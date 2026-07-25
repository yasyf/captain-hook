package hookd

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/yasyf/daemonkit/trust"
)

const (
	defaultRequestTimeout  = 30 * time.Second
	defaultShutdownTimeout = 60 * time.Second
)

// Main executes one capt-hookd client or host command and returns its exit code.
func Main(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprintln(stderr, "usage: capt-hookd version|serve|run|status|restart-workers|package-install|package-uninstall")
		return 2
	}
	// The serving daemon re-execs this binary as daemonkit's trust-verifier
	// child for every connecting peer; without this dispatch every peer is
	// rejected as untrusted.
	if handled, err := trust.RunVerifierChild(args, stdout); handled {
		if err != nil {
			fmt.Fprintf(stderr, "capt-hookd: %v\n", err)
			return 2
		}
		return 0
	}
	switch args[0] {
	case "version":
		return versionCommand(args[1:], stdout, stderr)
	case "serve":
		return serveCommand(args[1:], stderr)
	case "run":
		return runCommand(args[1:], stdin, stdout, stderr)
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
	}{Schema: Schema, Build: Build}); err != nil {
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
	flags := flag.NewFlagSet("run", flag.ContinueOnError)
	flags.SetOutput(stderr)
	event := flags.String("event", "", "hook event")
	root := flags.String("root", "", "project root")
	cwd := flags.String("cwd", "", "request working directory")
	python := flags.String("python", "", "exact Python executable")
	build := flags.String("build", "", "exact Python product build")
	async := flags.Bool("async", false, "dispatch async hooks")
	timeout := flags.Duration(
		"timeout", durationFromEnvironment("CAPT_HOOK_CLIENT_TIMEOUT", defaultRequestTimeout), "request deadline",
	)
	if err := flags.Parse(args); err != nil || flags.NArg() != 0 {
		return 2
	}
	if *cwd == "" {
		*cwd, _ = os.Getwd()
	}
	payload, err := io.ReadAll(io.LimitReader(stdin, maxEventInput+1))
	if err != nil {
		fmt.Fprintf(stderr, "capt-hookd: read event: %v\n", err)
		return 1
	}
	if len(payload) > maxEventInput {
		fmt.Fprintf(stderr, "capt-hookd: event input exceeds %d bytes\n", maxEventInput)
		return 1
	}
	request := EventRequest{
		Schema: Schema, Event: *event, Async: *async, Root: *root, CWD: *cwd,
		Env: requestEnvironment(os.Environ()), PayloadRaw: string(payload),
		Python: *python, Build: *build, ClientPID: os.Getpid(), ClientPPID: os.Getppid(),
	}
	if err := validateEventRequest(request); err != nil {
		fmt.Fprintln(stderr, err)
		return 2
	}
	if request.Build != Build {
		fmt.Fprintf(stderr, "capt-hookd: Python build %q does not match signed host build %q\n", request.Build, Build)
		return 1
	}
	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()
	client, err := NewClient()
	if err == nil {
		defer client.Close()
		err = client.EnsureCurrent(ctx, *timeout)
	}
	var response EventResponse
	if err == nil {
		response, err = client.Event(ctx, request)
	}
	if err != nil {
		fmt.Fprintln(stderr, err)
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
	ctx, cancel := context.WithTimeout(context.Background(), defaultShutdownTimeout)
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
	ctx, cancel := context.WithTimeout(context.Background(), defaultShutdownTimeout)
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
