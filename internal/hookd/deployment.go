package hookd

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"time"

	"github.com/yasyf/daemonkit"
	"github.com/yasyf/daemonkit/artifact"
	"github.com/yasyf/daemonkit/deploy"
	"github.com/yasyf/daemonkit/launchd"
)

const (
	helperBundleID        = "com.yasyf.capt-hook.helper"
	helperServiceLabel    = "com.yasyf.capt-hook.helper"
	helperApplicationName = "Captain Hook"
	helperApplicationLeaf = helperApplicationName + ".app"
	helperBridgeName      = "capt-hook-helper-client"
	hostBundleExecutable  = "Contents/Helpers/capt-hookd"
	bridgePingTimeout     = 8 * time.Second

	// appStopTimeout bounds the signed stop entrypoint, which asks the running
	// generation to terminate, waits five seconds, force-terminates what stayed,
	// and proves quiet absence after that.
	appStopTimeout = 20 * time.Second

	// hostStopTimeout must clear hostShutdownTimeout: a serving host is drained
	// through the grace its own LaunchAgent promises it before its agent comes
	// down, and a budget shorter than that grace would end the stop mid-drain.
	hostStopTimeout = hostShutdownTimeout + 15*time.Second
)

var strictMarketingVersion = regexp.MustCompile(`^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$`)

func helperApplicationRequirement() daemonkit.Requirement {
	return daemonkit.Requirement{TeamID: hostTeamID, SigningIdentifier: helperBundleID}
}

func marketingVersion() (string, error) {
	if !strictMarketingVersion.MatchString(Build) {
		return "", fmt.Errorf("captain package: build %q is not an exact marketing version", Build)
	}
	return Build, nil
}

func installedApplicationPath() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("captain package: resolve user home: %w", err)
	}
	resolved, err := filepath.EvalSymlinks(home)
	if err != nil || resolved != home {
		return "", errors.New("captain package: user home must be a canonical real directory")
	}
	applications := filepath.Join(home, "Applications")
	if err := ensureRealDirectory(applications); err != nil {
		return "", err
	}
	return filepath.Join(applications, helperApplicationLeaf), nil
}

func ensureRealDirectory(path string) error {
	info, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		if err := os.Mkdir(path, 0o755); err != nil {
			return fmt.Errorf("captain package: create %q: %w", path, err)
		}
		info, err = os.Lstat(path)
	}
	if err != nil {
		return fmt.Errorf("captain package: inspect %q: %w", path, err)
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return fmt.Errorf("captain package: %q is not a real directory", path)
	}
	return nil
}

// canonicalExecutable is this process's own image in the form the kernel
// reports one: absolute and symlink-free.
func canonicalExecutable() (string, error) {
	executable, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("captain package: resolve current executable: %w", err)
	}
	resolved, err := filepath.EvalSymlinks(executable)
	if err != nil {
		return "", fmt.Errorf("captain package: resolve %q: %w", executable, err)
	}
	return resolved, nil
}

func packagedApplicationPath() (string, error) {
	executable, err := canonicalExecutable()
	if err != nil {
		return "", err
	}
	app := filepath.Dir(filepath.Dir(filepath.Dir(executable)))
	if filepath.Base(app) != helperApplicationLeaf || executable != hostExecutablePath(app) {
		return "", fmt.Errorf("captain package: executable %q is not the packaged host", executable)
	}
	return app, nil
}

func hostExecutablePath(appPath string) string {
	return filepath.Join(appPath, hostBundleExecutable)
}

func appExecutablePath(appPath string) string {
	return filepath.Join(appPath, "Contents", "MacOS", helperApplicationName)
}

func bridgeExecutablePath(appPath string) string {
	return filepath.Join(appPath, "Contents", "Helpers", helperBridgeName)
}

// exactAgents is the LaunchAgent set activation converges launchd to. It is
// the only declaration of this daemon's launchd job: hostDaemon leaves Program
// unset because Client.Ensure never runs here, so this is also the only place
// the host's ExitTimeOut can be stated — launchd's own 20-second default would
// SIGKILL the host before the drain hostDaemon declares had run out.
func exactAgents(appPath string) ([]launchd.Agent, error) {
	resolved, err := resolvePaths()
	if err != nil {
		return nil, err
	}
	return []launchd.Agent{
		{
			Label: helperServiceLabel, Program: appExecutablePath(appPath),
			LogPath: resolved.log, RestartPolicy: launchd.RestartOnFailure,
			AssociatedBundleIdentifiers: []string{helperBundleID},
		},
		{
			Label: hostServiceLabel, Program: hostExecutablePath(appPath), Args: []string{"serve"},
			LogPath: resolved.log, RestartPolicy: launchd.RestartOnFailure,
			ExitTimeOut:                 hostShutdownTimeout,
			ProcessType:                 launchd.ProcessTypeInteractive,
			AssociatedBundleIdentifiers: []string{helperBundleID},
		},
	}, nil
}

func openDeployment(appPath string) (*deploy.Deployment, error) {
	agents, err := exactAgents(appPath)
	if err != nil {
		return nil, err
	}
	return deploy.Open(deploy.Config{
		App:         appPath,
		Requirement: helperApplicationRequirement(),
		Daemon:      hostDaemon(),
		Agents:      agents,
	})
}

func applyPackagedApplication(ctx context.Context) error {
	store, err := artifact.DefaultStore()
	if err != nil {
		return err
	}
	if _, err := store.Resolve(ctx, productToolDescriptor()); err != nil {
		return fmt.Errorf("captain package: install the capt-hook %s tool env: %w", Build, err)
	}
	source, err := packagedApplicationPath()
	if err != nil {
		return err
	}
	targetPath, err := installedApplicationPath()
	if err != nil {
		return err
	}
	if source == targetPath {
		return errors.New("captain package: packaged source and installed target must differ")
	}
	version, err := marketingVersion()
	if err != nil {
		return err
	}
	digest, err := deploy.BundleDigest(source)
	if err != nil {
		return err
	}
	deployment, err := openDeployment(targetPath)
	if err != nil {
		return fmt.Errorf("captain package: open deployment: %w", err)
	}
	candidate := deploy.Candidate{Source: source, Version: version, Digest: digest}
	land := deployment.Install
	if _, err := os.Lstat(targetPath); err == nil {
		land = deployment.Supersede
		if err := quiesceInstalledApplication(ctx, source, targetPath); err != nil {
			return fmt.Errorf("%w; nothing was applied and %s still serves the previous generation", err, targetPath)
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("captain package: inspect %q: %w", targetPath, err)
	}
	if _, err := land(ctx, candidate); err != nil {
		return fmt.Errorf("captain package: land delivered app: %w", err)
	}
	activation, err := deployment.Activate(ctx)
	if err != nil {
		return fmt.Errorf("captain package: activate installed app: %w", err)
	}
	if err := validateActivation(activation, targetPath, version); err != nil {
		return err
	}
	return pingBridge(ctx, targetPath)
}

func uninstallPackagedApplication(ctx context.Context) error {
	targetPath, err := installedApplicationPath()
	if err != nil {
		return err
	}
	if _, err := os.Lstat(targetPath); err == nil {
		source, err := packagedApplicationPath()
		if err != nil {
			return err
		}
		if err := quiesceInstalledApplication(ctx, source, targetPath); err != nil {
			return err
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("captain package: inspect %q: %w", targetPath, err)
	}
	if err := stopInstalledHost(ctx); err != nil {
		return err
	}
	deployment, err := openDeployment(targetPath)
	if err != nil {
		return fmt.Errorf("captain package: open deployment: %w", err)
	}
	removal, err := deployment.Uninstall(ctx)
	if err != nil {
		return fmt.Errorf("captain package: uninstall installed app: %w", err)
	}
	if !removal.Runtime.Absent() || removal.Runtime.Digest() == (deploy.SHA256{}) {
		return errors.New("captain package: daemonkit returned an inexact absence proof")
	}
	return validateGeneration(removal.Generation, targetPath, removal.Generation.Version)
}

func validateActivation(activation deploy.Activation, appPath, version string) error {
	if err := validateGeneration(activation.Generation, appPath, version); err != nil {
		return err
	}
	if activation.Readiness.Build() == "" || activation.Readiness.Generation() == 0 ||
		activation.Readiness.Digest() == (deploy.SHA256{}) {
		return errors.New("captain package: daemonkit returned an inexact readiness proof")
	}
	return nil
}

func validateGeneration(generation deploy.Generation, appPath, version string) error {
	if generation.Path != appPath || generation.Version != version ||
		generation.TeamID != hostTeamID || generation.SigningIdentifier != helperBundleID ||
		generation.CDHash == "" || generation.BundleDigest == "" || generation.EntitlementsDigest == "" ||
		generation.FileID == (deploy.FileID{}) {
		return errors.New("captain package: deployment receipt names a different app generation")
	}
	return nil
}

// quiesceInstalledApplication stops the helper generation running out of the
// installed bundle. Supersede and Uninstall both inventory every executable in
// that bundle and refuse with deploy.ErrLive while any of them is alive, and
// the app half is not the daemon daemonkit drains — so the app is stopped here,
// through the signed entrypoint that terminates the exact generation at a named
// path and proves quiet absence before exiting. Finding nothing to stop is that
// proof, not a failure.
//
// The controller is the packaged app's own binary, never the installed one:
// running the installed executable would put a live process inside the very
// bundle the inventory has to find empty.
func quiesceInstalledApplication(ctx context.Context, controllerApp, installedApp string) (err error) {
	resolved, err := resolvePaths()
	if err != nil {
		return err
	}
	if err := resolved.ensure(); err != nil {
		return err
	}
	owned, err := daemonkit.OwnProcesses(ctx, resolved.deploymentRecords)
	if err != nil {
		return fmt.Errorf("captain package: own deployment processes: %w", err)
	}
	defer func() {
		closeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), closeTimeout)
		defer cancel()
		err = errors.Join(err, owned.Close(closeCtx))
	}()
	runCtx, cancel := context.WithTimeout(ctx, appStopTimeout)
	defer cancel()
	controller := appExecutablePath(controllerApp)
	result, err := owned.Run(runCtx, daemonkit.Cmd{
		Path: controller, Dir: filepath.Dir(controller),
		Args: []string{"--deployment-stop-installed-generation", installedApp},
		Exec: daemonkit.ServingSigned(helperApplicationRequirement()), MaxOutput: 4 << 10,
	})
	if err != nil {
		return fmt.Errorf(
			"captain package: stop installed app generation: %w: controller stderr %q",
			err, bytes.TrimSpace(result.Stderr),
		)
	}
	if len(result.Stderr) != 0 {
		return fmt.Errorf("captain package: stopping the installed app wrote stderr: %q", string(result.Stderr))
	}
	return nil
}

// stopInstalledHost makes nothing serve the host label and takes its agent
// down, draining the incumbent through the control lane.
func stopInstalledHost(ctx context.Context) error {
	client, err := daemonkit.Open(hostDaemon())
	if err != nil {
		return fmt.Errorf("captain package: open signed host: %w", err)
	}
	stopCtx, cancel := context.WithTimeout(ctx, hostStopTimeout)
	defer cancel()
	if err := client.Stop(stopCtx); err != nil {
		return fmt.Errorf("captain package: stop installed host: %w", err)
	}
	return nil
}

// pingBridge proves the signed broker in the activated bundle answers with
// this build. It runs after Activate rather than inside it: v0.21 seals
// readiness from the daemon's own published health and takes no consumer hook.
func pingBridge(ctx context.Context, appPath string) (err error) {
	resolved, err := resolvePaths()
	if err != nil {
		return err
	}
	if err := resolved.ensure(); err != nil {
		return err
	}
	owned, err := daemonkit.OwnProcesses(ctx, resolved.deploymentRecords)
	if err != nil {
		return fmt.Errorf("captain package: own deployment processes: %w", err)
	}
	defer func() {
		closeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), closeTimeout)
		defer cancel()
		err = errors.Join(err, owned.Close(closeCtx))
	}()
	runCtx, cancel := context.WithTimeout(ctx, bridgePingTimeout)
	defer cancel()
	bridge := bridgeExecutablePath(appPath)
	result, err := owned.Run(runCtx, daemonkit.Cmd{
		Path: bridge, Dir: filepath.Dir(bridge), Args: []string{"ping"},
		Exec: daemonkit.ServingSigned(helperClientRequirement()), MaxOutput: 4 << 10,
	})
	if err != nil {
		return fmt.Errorf("captain package: broker ping: %w: bridge stderr %q", err, bytes.TrimSpace(result.Stderr))
	}
	if len(result.Stderr) != 0 {
		return fmt.Errorf("captain package: broker ping wrote stderr: %q", string(result.Stderr))
	}
	var reply helperReply
	if err := decodeStrict(result.Stdout, &reply); err != nil {
		return fmt.Errorf("captain package: decode broker ping: %w", err)
	}
	if !reply.OK || reply.Version == nil || *reply.Version != Build {
		return errors.New("captain package: broker ping returned a different build")
	}
	return nil
}
