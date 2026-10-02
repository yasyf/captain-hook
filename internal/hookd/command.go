package hookd

import (
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
	"github.com/yasyf/daemonkit"
)

const (
	defaultRequestTimeout = 30 * time.Second

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
		fmt.Fprintln(stderr, "usage: capt-hookd version|serve|supervise|run|transcript-client|status|restart-workers|package-install|package-uninstall")
		return 2
	}
	switch args[0] {
	case "version", "--version":
		return versionCommand(args[1:], stdout, stderr)
	case "serve":
		return serveCommand(args[1:], stderr)
	case "supervise":
		return superviseCommand(args[1:], stderr)
	case "transcript-client":
		return transcriptClientCommand(args[1:], stdin, stdout, stderr)
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
	if err := json.NewEncoder(stdout).Encode(currentHostVersion()); err != nil {
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

func superviseCommand(args []string, stderr io.Writer) int {
	if len(args) != 0 {
		fmt.Fprintln(stderr, "capt-hookd supervise: no arguments accepted")
		return 2
	}
	if err := superviseHost(context.Background()); err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	return 0
}

func runCommand(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) == 2 && args[1] == "--async" {
		return 0
	}
	if len(args) != 1 || args[0] == "" || strings.HasPrefix(args[0], "-") {
		fmt.Fprintln(stderr, "usage: capt-hookd run EVENT")
		return 1
	}
	if p, paused := activePause(time.Now()); paused {
		if args[0] == "SessionStart" {
			if _, err := io.WriteString(stdout, pauseBanner(p)); err != nil {
				fmt.Fprintf(stderr, "capt-hookd: write result: %v\n", err)
				return 1
			}
		}
		return 0
	}
	timeout := durationFromEnvironment("CAPT_HOOK_CLIENT_TIMEOUT", defaultRequestTimeout)
	if timeout <= 0 {
		fmt.Fprintf(stderr, "capt-hookd: request timeout %s must be positive\n", timeout)
		return 1
	}
	request, err := eventRequest(args[0], stdin)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	request.Mandatory = wireproto.Mandatory(request.Event, []byte(request.PayloadRaw))
	client, err := openEventClient()
	var response wireproto.EventResponse
	if err == nil {
		defer client.Close()
		response, err = dispatch(client, request, timeout)
		if err != nil && request.Mandatory && failureKind(err, true) == "transport-timeout" {
			response, err = dispatch(client, request, timeout)
		}
	}
	if err != nil {
		if request.Mandatory {
			return denyMandatory(stdout, stderr, request.Event, failureKind(err, client != nil))
		}
		fmt.Fprintln(stderr, err)
		return 1
	}
	if request.Mandatory {
		if kind := incompleteKind(response); kind != "" {
			return denyMandatory(stdout, stderr, request.Event, kind)
		}
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

// eventClient is the host session runCommand dispatches through.
type eventClient interface {
	Event(ctx context.Context, request wireproto.EventRequest) (wireproto.EventResponse, error)
	Close() error
}

// openEventClient opens the host session.
var openEventClient = func() (eventClient, error) {
	client, err := NewClient()
	if err != nil {
		return nil, err
	}
	return client, nil
}

// denyMandatory answers a mandatory event whose guard did not complete: the
// deny envelope on stdout, the kind alone on stderr, and exit 0 so Claude Code
// reads the verdict rather than a hook error it would let the call through on.
func denyMandatory(stdout, stderr io.Writer, event, kind string) int {
	if _, err := io.WriteString(stdout, wireproto.DenyEnvelope(event, kind)+"\n"); err != nil {
		fmt.Fprintf(stderr, "capt-hookd: write result: %v\n", err)
		return 1
	}
	fmt.Fprintf(stderr, "capt-hookd: the session guard did not complete (%s); denied\n", kind)
	return 0
}

func dispatch(client eventClient, request wireproto.EventRequest, timeout time.Duration) (wireproto.EventResponse, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	return client.Event(ctx, request)
}

func failureKind(err error, opened bool) string {
	switch {
	case !opened:
		return "host-unavailable"
	case errors.Is(err, daemonkit.ErrAbsent) || errors.Is(err, daemonkit.ErrDraining) ||
		errors.Is(err, daemonkit.ErrNotReady) || errors.Is(err, daemonkit.ErrSessionCapacity):
		return "transport-refused"
	case errors.Is(err, context.DeadlineExceeded):
		return "transport-timeout"
	default:
		return "transport-error"
	}
}

func incompleteKind(response wireproto.EventResponse) string {
	switch {
	case response.Exit != 0:
		return "worker-error"
	case response.Guard == wireproto.GuardCompleted:
		return ""
	case strings.Contains(response.Stderr, "no verdict"):
		return "shed"
	default:
		return "no-verdict"
	}
}

func eventRequest(event string, stdin io.Reader) (wireproto.EventRequest, error) {
	payload, err := io.ReadAll(io.LimitReader(stdin, wireproto.MaxEventInput+1))
	if err != nil {
		return wireproto.EventRequest{}, fmt.Errorf("capt-hookd: read event: %w", err)
	}
	if len(payload) > wireproto.MaxEventInput {
		return wireproto.EventRequest{}, fmt.Errorf("capt-hookd: event input exceeds %d bytes", wireproto.MaxEventInput)
	}
	var fields struct {
		CWD string `json:"cwd"`
	}
	if err := json.Unmarshal(payload, &fields); err != nil {
		return wireproto.EventRequest{}, fmt.Errorf("capt-hookd: decode event: %w", err)
	}
	request := wireproto.EventRequest{
		Schema: wireproto.Schema, Event: event,
		Root: cmp.Or(os.Getenv("CLAUDE_PROJECT_DIR"), os.Getenv("FACTORY_PROJECT_DIR"), fields.CWD),
		CWD:  fields.CWD, Env: requestEnvironment(os.Environ()), PayloadRaw: string(payload),
		ClientPID: os.Getpid(), ClientPPID: os.Getppid(),
	}
	return request, request.Validate()
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
		if name == "XDG_CACHE_HOME" || name == "CEREBRAS_API_KEY" || strings.HasPrefix(name, "CAPT_HOOK_") ||
			strings.HasPrefix(name, "CAPTAIN_HOOK_") || strings.HasPrefix(name, "HOOKS_") ||
			strings.HasPrefix(name, "CLAUDE_") || strings.HasPrefix(name, "FACTORY_") || strings.HasPrefix(name, "ORCA_") {
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
