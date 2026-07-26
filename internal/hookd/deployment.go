package hookd

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
	"time"

	"github.com/yasyf/daemonkit/codeidentity"
	"github.com/yasyf/daemonkit/deployment"
	"github.com/yasyf/daemonkit/proc"
	"github.com/yasyf/daemonkit/service"
	"github.com/yasyf/daemonkit/trust"
	"github.com/yasyf/daemonkit/worker"
)

const (
	helperBundleID          = "com.yasyf.capt-hook.helper"
	helperServiceLabel      = "com.yasyf.capt-hook.helper"
	helperApplicationName   = "Captain Hook"
	helperApplicationLeaf   = helperApplicationName + ".app"
	helperBridgeName        = "capt-hook-helper-client"
	deploymentPolicyID      = "captain-hook.deployment-policy.v1"
	deploymentProofID       = "captain-hook.deployment-proof.v1"
	deploymentConsumerID    = "captain-hook.deployment-consumer.v1@sha256:"
	deploymentDaemonkitLine = "0.20.8"
)

var strictMarketingVersion = regexp.MustCompile(`^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$`)

type deploymentController interface {
	AttestInstalled(context.Context, deployment.InstalledSpec) (deployment.InstalledAttestation, error)
	ApplyInstalledCandidate(context.Context, deployment.ApplyInstalledCandidateConfig) (deployment.ApplyInstalledCandidateReceipt, error)
	UninstallCurrentInstalled(context.Context, deployment.UninstallCurrentInstalledConfig) (deployment.UninstallReceipt, error)
}

var newDeploymentController = func() deploymentController { return deployment.New() }

type deploymentPolicy struct {
	Identity    string                      `json:"identity"`
	Schema      int                         `json:"schema"`
	Daemonkit   string                      `json:"daemonkit"`
	Application deploymentApplicationPolicy `json:"application"`
	Protocol    deploymentProtocolPolicy    `json:"protocol"`
	Services    []deploymentServicePolicy   `json:"services"`
	Quiescence  deploymentQuiescencePolicy  `json:"quiescence"`
	Readiness   deploymentReadinessPolicy   `json:"readiness"`
}

type deploymentApplicationPolicy struct {
	BundleID                string   `json:"bundle_id"`
	TeamID                  string   `json:"team_id"`
	InstallRootHomeRelative string   `json:"install_root_home_relative"`
	BundleLeaf              string   `json:"bundle_leaf"`
	Executables             []string `json:"executables"`
	NoSystemApplications    bool     `json:"no_system_applications"`
	NoSeparateCask          bool     `json:"no_separate_cask"`
}

type deploymentProtocolPolicy struct {
	Schema    int    `json:"schema"`
	WireBuild string `json:"wire_build"`
}

type deploymentServicePolicy struct {
	Label     string                `json:"label"`
	Program   string                `json:"program"`
	Arguments []string              `json:"arguments"`
	Restart   service.RestartPolicy `json:"restart"`
}

type deploymentQuiescencePolicy struct {
	StopRole                 string `json:"stop_role"`
	RequireExactStopReceipt  bool   `json:"require_exact_stop_receipt"`
	RequireEmptyInventories  bool   `json:"require_empty_inventories"`
	RequireGenerationBinding bool   `json:"require_generation_binding"`
}

type deploymentReadinessPolicy struct {
	RequireExactPlan         bool `json:"require_exact_plan"`
	RequireRuntimeHealth     bool `json:"require_runtime_health"`
	RequireSignedBrokerPing  bool `json:"require_signed_broker_ping"`
	RequireExactInventories  bool `json:"require_exact_inventories"`
	RequireAttestationDigest bool `json:"require_attestation_digest"`
}

func helperCodeIdentity() codeidentity.CodeIdentity {
	return codeidentity.CodeIdentity{TeamID: hostTeamID, SigningIdentifier: helperBundleID}
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

func packagedApplicationPath() (string, error) {
	executable, err := service.CanonicalExecutable()
	if err != nil {
		return "", fmt.Errorf("captain package: resolve packaged executable: %w", err)
	}
	app := filepath.Dir(filepath.Dir(filepath.Dir(executable)))
	if filepath.Base(app) != helperApplicationLeaf ||
		executable != hostExecutablePath(app) {
		return "", fmt.Errorf("captain package: executable %q is not the packaged host", executable)
	}
	return app, nil
}

func hostExecutablePath(appPath string) string {
	return filepath.Join(appPath, "Contents", "Helpers", "capt-hookd")
}

func appExecutablePath(appPath string) string {
	return filepath.Join(appPath, "Contents", "MacOS", helperApplicationName)
}

func bridgeExecutablePath(appPath string) string {
	return filepath.Join(appPath, "Contents", "Helpers", helperBridgeName)
}

func exactServicePlan(appPath string) (service.Plan, error) {
	resolved, err := resolvePaths()
	if err != nil {
		return service.Plan{}, err
	}
	return service.NewPlan([]service.Agent{
		{
			Label: helperServiceLabel, Program: appExecutablePath(appPath),
			LogPath: resolved.log, RestartPolicy: service.RestartOnFailure,
			AssociatedBundleIdentifiers: []string{helperBundleID},
		},
		{
			Label: hostServiceLabel, Program: hostExecutablePath(appPath), Args: []string{"serve"},
			LogPath: resolved.log, RestartPolicy: service.RestartOnFailure,
			AssociatedBundleIdentifiers: []string{helperBundleID},
		},
	})
}

func exactCandidatePlan(source string) (deployment.CandidatePlan, error) {
	plan, err := exactServicePlan(source)
	if err != nil {
		return deployment.CandidatePlan{}, err
	}
	return deployment.NewCandidatePlan(source, plan.Agents())
}

func deploymentIdentity() (string, deployment.SHA256, error) {
	executable, err := service.CanonicalExecutable()
	if err != nil {
		return "", deployment.SHA256{}, err
	}
	file, err := os.Open(executable)
	if err != nil {
		return "", deployment.SHA256{}, err
	}
	digest := sha256.New()
	if _, err := io.Copy(digest, file); err != nil {
		_ = file.Close()
		return "", deployment.SHA256{}, err
	}
	if err := file.Close(); err != nil {
		return "", deployment.SHA256{}, err
	}
	consumer := deploymentConsumerID + hex.EncodeToString(digest.Sum(nil))
	policy := deploymentPolicy{
		Identity: deploymentPolicyID, Schema: 1, Daemonkit: deploymentDaemonkitLine,
		Application: deploymentApplicationPolicy{
			BundleID: helperBundleID, TeamID: hostTeamID,
			InstallRootHomeRelative: "Applications", BundleLeaf: helperApplicationLeaf,
			Executables: []string{
				"Contents/MacOS/" + helperApplicationName,
				"Contents/Helpers/capt-hookd",
				"Contents/Helpers/" + helperBridgeName,
			},
			NoSystemApplications: true, NoSeparateCask: true,
		},
		Protocol: deploymentProtocolPolicy{Schema: Schema, WireBuild: WireBuild},
		Services: []deploymentServicePolicy{
			{
				Label: helperServiceLabel, Program: "Contents/MacOS/" + helperApplicationName,
				Restart: service.RestartOnFailure,
			},
			{
				Label: hostServiceLabel, Program: "Contents/Helpers/capt-hookd",
				Arguments: []string{"serve"}, Restart: service.RestartOnFailure,
			},
		},
		Quiescence: deploymentQuiescencePolicy{
			StopRole: string(stopControlRoleID), RequireExactStopReceipt: true,
			RequireEmptyInventories: true, RequireGenerationBinding: true,
		},
		Readiness: deploymentReadinessPolicy{
			RequireExactPlan: true, RequireRuntimeHealth: true, RequireSignedBrokerPing: true,
			RequireExactInventories: true, RequireAttestationDigest: true,
		},
	}
	payload, err := json.Marshal(policy)
	if err != nil {
		return "", deployment.SHA256{}, err
	}
	return consumer, deployment.SHA256(sha256.Sum256(payload)), nil
}

func applyPackagedApplication(ctx context.Context) error {
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
	controller := newDeploymentController()
	candidate, err := controller.AttestInstalled(ctx, deployment.InstalledSpec{
		AppPath: source, Version: version, Identity: helperCodeIdentity(),
	})
	if err != nil {
		return fmt.Errorf("captain package: attest delivered app: %w", err)
	}
	candidatePlan, err := exactCandidatePlan(source)
	if err != nil {
		return fmt.Errorf("captain package: bind service plan: %w", err)
	}
	consumer, policy, err := deploymentIdentity()
	if err != nil {
		return fmt.Errorf("captain package: deployment identity: %w", err)
	}
	hooks := newDeploymentHooks(policy)
	target := deployment.CurrentInstalledSpec{AppPath: targetPath, Identity: helperCodeIdentity()}
	receipt, err := controller.ApplyInstalledCandidate(ctx, deployment.ApplyInstalledCandidateConfig{
		Target: target, CandidateSourcePath: source, CandidateVersion: version,
		CandidateBundleDigest: candidate.BundleDigest(), ConsumerBuild: consumer,
		PolicyDigest: policy, Plan: candidatePlan,
		RuntimeQuiesce: hooks.runtimeQuiesce, Readiness: hooks.readiness,
	})
	if err != nil {
		return fmt.Errorf("captain package: apply delivered app: %w", err)
	}
	if !validDeploymentOperationID(receipt.OperationID()) {
		return errors.New("captain package: daemonkit returned an inexact apply receipt")
	}
	installed, err := controller.AttestInstalled(ctx, deployment.InstalledSpec{
		AppPath: targetPath, Version: version, Identity: helperCodeIdentity(),
	})
	if err != nil {
		return fmt.Errorf("captain package: attest installed app: %w", err)
	}
	plan, err := exactServicePlan(targetPath)
	if err != nil {
		return err
	}
	return validateActivationReceipt(receipt.Activation(), installed, plan)
}

func uninstallPackagedApplication(ctx context.Context) error {
	targetPath, err := installedApplicationPath()
	if err != nil {
		return err
	}
	_, policy, err := deploymentIdentity()
	if err != nil {
		return err
	}
	target := deployment.CurrentInstalledSpec{AppPath: targetPath, Identity: helperCodeIdentity()}
	hooks := newDeploymentHooks(policy)
	receipt, err := newDeploymentController().UninstallCurrentInstalled(
		ctx,
		deployment.UninstallCurrentInstalledConfig{
			Current: target, RuntimeQuiesce: hooks.runtimeQuiesce, Readiness: hooks.readiness,
		},
	)
	if err != nil {
		return fmt.Errorf("captain package: uninstall installed app: %w", err)
	}
	if !validDeploymentOperationID(receipt.OperationID()) ||
		!validDeploymentOperationID(receipt.DeactivationOperationID()) ||
		!receipt.RuntimeProof().Absent() ||
		receipt.RuntimeProof().Digest() == (deployment.SHA256{}) {
		return errors.New("captain package: daemonkit returned an inexact uninstall receipt")
	}
	generation := receipt.Generation()
	if generation.Path() != targetPath || generation.TeamID() != hostTeamID ||
		generation.SigningIdentifier() != helperBundleID ||
		generation.BundleDigest() == (deployment.SHA256{}) ||
		generation.EntitlementsDigest() == (deployment.SHA256{}) {
		return errors.New("captain package: uninstall receipt names a different app generation")
	}
	return nil
}

func validateActivationReceipt(
	receipt deployment.ActivationReceipt,
	want deployment.InstalledAttestation,
	plan service.Plan,
) error {
	readiness, ready := receipt.Readiness()
	if !receipt.Active() || !ready || !validDeploymentOperationID(receipt.OperationID()) ||
		!sameAttestation(receipt.Generation(), want) ||
		receipt.Plan().Digest() != plan.Digest() ||
		!reflect.DeepEqual(receipt.Plan().Agents(), plan.Agents()) ||
		readiness.RuntimeBuild() != Build ||
		readiness.ProcessGeneration() == (proc.OwnerGeneration{}) ||
		readiness.ResourceDigest() == (deployment.SHA256{}) {
		return errors.New("captain package: daemonkit returned an inexact activation receipt")
	}
	return nil
}

func sameAttestation(left, right deployment.InstalledAttestation) bool {
	return left.Path() == right.Path() && left.Version() == right.Version() &&
		left.TeamID() == right.TeamID() &&
		left.SigningIdentifier() == right.SigningIdentifier() &&
		left.DesignatedRequirement() == right.DesignatedRequirement() &&
		left.CDHash() == right.CDHash() &&
		left.BundleDigest() == right.BundleDigest() &&
		left.EntitlementsDigest() == right.EntitlementsDigest() &&
		left.Device() == right.Device() && left.Inode() == right.Inode()
}

func validDeploymentOperationID(value string) bool {
	if len(value) != 64 || value != strings.ToLower(value) {
		return false
	}
	decoded, err := hex.DecodeString(value)
	if err != nil || len(decoded) != 32 {
		return false
	}
	for _, octet := range decoded {
		if octet != 0 {
			return true
		}
	}
	return false
}

type deploymentHooks struct {
	policy deployment.SHA256
	paths  paths
}

func newDeploymentHooks(policy deployment.SHA256) deploymentHooks {
	resolved, _ := resolvePaths()
	return deploymentHooks{policy: policy, paths: resolved}
}

func (h deploymentHooks) readiness(
	ctx context.Context,
	operation deployment.InstalledOperation,
) (deployment.ReadinessProof, error) {
	generation := operation.Generation()
	want, err := exactServicePlan(generation.Path())
	if err != nil {
		return deployment.ReadinessProof{}, err
	}
	if operation.Plan().Digest() != want.Digest() ||
		!reflect.DeepEqual(operation.Plan().Agents(), want.Agents()) {
		return deployment.ReadinessProof{}, errors.New("captain package: readiness plan is not exact")
	}
	readyCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	var lastErr error
	for {
		client := newClientWithPaths(h.paths)
		health, healthErr := client.RuntimeHealth(readyCtx)
		if healthErr == nil && health.current() {
			processGeneration, parseErr := proc.ParseOwnerGeneration(health.ProcessGeneration)
			if parseErr != nil {
				_ = client.Close()
				return deployment.ReadinessProof{}, fmt.Errorf("captain package: parse runtime generation: %w", parseErr)
			}
			bridge, bridgeErr := h.runBridge(readyCtx, generation.Path())
			hostProcesses, hostErr := proc.ExecutableIdentities(hostExecutablePath(generation.Path()))
			appProcesses, appErr := proc.ExecutableIdentities(appExecutablePath(generation.Path()))
			_ = client.Close()
			if bridgeErr == nil && bridge.OK && bridge.Version != nil && *bridge.Version == Build &&
				hostErr == nil && appErr == nil && len(hostProcesses) == 1 && len(appProcesses) == 1 {
				return deployment.NewReadinessProof(
					Build, processGeneration,
					h.proofDigest(
						"runtime-ready", operation.OperationID(), generation, operation.Plan(),
						health.ProcessGeneration, generation.BundleDigest().String(),
						generation.EntitlementsDigest().String(), fmt.Sprintf("%d", len(hostProcesses)),
						fmt.Sprintf("%d", len(appProcesses)),
					),
				)
			}
			lastErr = errors.Join(bridgeErr, hostErr, appErr)
			if bridgeErr == nil && (bridge.Version == nil || *bridge.Version != Build) {
				lastErr = errors.New("captain package: broker ping returned a different build")
			}
		} else {
			_ = client.Close()
			lastErr = healthErr
			if healthErr == nil {
				lastErr = errors.New("captain package: runtime is not exactly ready")
			}
		}
		select {
		case <-readyCtx.Done():
			return deployment.ReadinessProof{}, fmt.Errorf(
				"captain package: wait for exact app readiness: %w",
				errors.Join(readyCtx.Err(), lastErr),
			)
		case <-time.After(100 * time.Millisecond):
		}
	}
}

func (h deploymentHooks) runtimeQuiesce(
	ctx context.Context,
	stopper deployment.RuntimeStopper,
	operation deployment.DeactivateInstalledOperation,
) (deployment.RuntimeProof, error) {
	activation := operation.Activation()
	generation := activation.Generation()
	want, err := exactServicePlan(generation.Path())
	if err != nil {
		return deployment.RuntimeProof{}, err
	}
	if activation.Plan().Digest() != want.Digest() ||
		!reflect.DeepEqual(activation.Plan().Agents(), want.Agents()) {
		return deployment.RuntimeProof{}, errors.New("captain package: quiesce plan is not exact")
	}
	var stopped proc.OwnerGeneration
	var receiptDetails []string
	client := newClientWithPaths(h.paths)
	health, observeErr := client.RuntimeHealth(ctx)
	if observeErr == nil {
		if !health.current() {
			_ = client.Close()
			return deployment.RuntimeProof{}, errors.New("captain package: prior runtime has the wrong generation")
		}
		stopped, err = proc.ParseOwnerGeneration(health.ProcessGeneration)
		if err != nil {
			_ = client.Close()
			return deployment.RuntimeProof{}, err
		}
		receipt, stopErr := stopper.StopRuntime(ctx, service.StopRuntimeRequest{
			OperationID:          operation.OperationID(),
			RuntimeClientConfig:  client.runtimeClientConfig(stopControlRoleID, 30*time.Second),
			ExpectedRuntimeBuild: health.RuntimeBuild, ControlRole: stopControlRoleID,
		})
		_ = client.Close()
		if stopErr != nil {
			return deployment.RuntimeProof{}, fmt.Errorf("captain package: stop exact host: %w", stopErr)
		}
		if receipt.OperationID() != operation.OperationID() ||
			receipt.Target().RuntimeBuild != health.RuntimeBuild ||
			receipt.Target().ProcessGeneration != stopped ||
			receipt.ProcessRecordDigest() == (proc.RecordDigest{}) ||
			receipt.Settlement() != service.StopSettlementGone ||
			receipt.Digest() == (service.StopReceiptDigest{}) {
			return deployment.RuntimeProof{}, errors.New("captain package: host stop receipt is not exact")
		}
		receiptDetails = []string{
			health.ProcessGeneration,
			fmt.Sprintf("%x", receipt.ProcessRecordDigest()),
			fmt.Sprintf("%x", receipt.Digest()),
		}
	} else {
		_ = client.Close()
		hostProcesses, inventoryErr := proc.ExecutableIdentities(hostExecutablePath(generation.Path()))
		if inventoryErr != nil || len(hostProcesses) != 0 {
			return deployment.RuntimeProof{}, fmt.Errorf(
				"captain package: host endpoint unavailable without exact absence: %w",
				errors.Join(observeErr, inventoryErr),
			)
		}
		receiptDetails = []string{"already-absent"}
	}
	if err := h.stopApplication(ctx, generation.Path()); err != nil {
		return deployment.RuntimeProof{}, err
	}
	hostProcesses, hostErr := proc.ExecutableIdentities(hostExecutablePath(generation.Path()))
	appProcesses, appErr := proc.ExecutableIdentities(appExecutablePath(generation.Path()))
	if hostErr != nil || appErr != nil || len(hostProcesses) != 0 || len(appProcesses) != 0 {
		return deployment.RuntimeProof{}, fmt.Errorf(
			"captain package: exact runtime inventory remains after quiescence: %w",
			errors.Join(hostErr, appErr),
		)
	}
	return deployment.NewRuntimeProof(
		true, stopped,
		h.proofDigest(
			"runtime-absent", operation.OperationID(), generation, activation.Plan(),
			receiptDetails...,
		),
	)
}

func (h deploymentHooks) stopApplication(ctx context.Context, appPath string) error {
	controllerApp, err := packagedApplicationPath()
	if err != nil {
		return err
	}
	result, err := h.runDeploymentTool(ctx, worker.CommandRequest{
		Path:         appExecutablePath(controllerApp),
		Dir:          filepath.Dir(appExecutablePath(controllerApp)),
		Args:         []string{"--deployment-stop-installed-generation", appPath},
		TotalTimeout: 10 * time.Second,
	})
	if err != nil {
		return fmt.Errorf("captain package: stop exact app generation: %w", err)
	}
	if result.ExitCode != 0 || len(result.Stderr) != 0 {
		return fmt.Errorf(
			"captain package: exact app stop failed: exit=%d stderr=%q",
			result.ExitCode, string(result.Stderr),
		)
	}
	executable := appExecutablePath(appPath)
	identities, err := proc.ExecutableIdentities(executable)
	if err != nil {
		return fmt.Errorf("captain package: inventory app process: %w", err)
	}
	if len(identities) != 0 {
		return fmt.Errorf("captain package: %d exact app process(es) remain", len(identities))
	}
	return nil
}

func (h deploymentHooks) runBridge(ctx context.Context, appPath string) (helperReply, error) {
	result, err := h.runDeploymentTool(ctx, worker.CommandRequest{
		Path: bridgeExecutablePath(appPath), Dir: filepath.Dir(bridgeExecutablePath(appPath)),
		Args:         []string{"ping"},
		TotalTimeout: 8 * time.Second,
	})
	if err != nil {
		return helperReply{}, err
	}
	if result.ExitCode != 0 || len(result.Stderr) != 0 {
		return helperReply{}, fmt.Errorf(
			"captain package: broker ping failed: exit=%d stderr=%q",
			result.ExitCode, string(result.Stderr),
		)
	}
	var reply helperReply
	if err := decodeStrict(result.Stdout, &reply); err != nil {
		return helperReply{}, fmt.Errorf("captain package: decode broker ping: %w", err)
	}
	return reply, nil
}

func (h deploymentHooks) runDeploymentTool(
	ctx context.Context,
	request worker.CommandRequest,
) (worker.CommandResult, error) {
	if err := h.paths.ensure(); err != nil {
		return worker.CommandResult{}, err
	}
	generation, err := proc.ProcessGeneration()
	if err != nil {
		return worker.CommandResult{}, err
	}
	reaper := &proc.Reaper{
		Store: &proc.FileStore{
			Path: h.paths.deploymentProcesses, UnsupportedSchema: proc.ArchiveUnsupportedSchema,
		},
		Generation: generation,
	}
	pool, err := worker.NewPool(worker.Config{
		Capacity: 1, QueueCapacity: 1, MaxTotalRun: 12 * time.Second,
		MaxStdinBytes: 1, MaxStdoutBytes: 4 << 10, MaxStderrBytes: 4 << 10,
	}, reaper)
	if err != nil {
		return worker.CommandResult{}, err
	}
	claim, err := pool.ClaimRuntime(trust.VerifierWorkerBudgets())
	if err != nil {
		return worker.CommandResult{}, err
	}
	if err := claim.Recover(ctx); err != nil {
		_ = claim.Close(context.WithoutCancel(ctx))
		return worker.CommandResult{}, err
	}
	if err := claim.Activate(); err != nil {
		_ = claim.Close(context.WithoutCancel(ctx))
		return worker.CommandResult{}, err
	}
	result, runErr := claim.Product().Run(ctx, request)
	closeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	closeErr := claim.Close(closeCtx)
	cancel()
	if runErr != nil || closeErr != nil {
		return worker.CommandResult{}, errors.Join(runErr, closeErr)
	}
	return result, nil
}

func (h deploymentHooks) proofDigest(
	kind, operationID string,
	generation deployment.InstalledAttestation,
	plan service.Plan,
	details ...string,
) deployment.SHA256 {
	digest := sha256.New()
	values := []string{
		deploymentProofID, kind, operationID, plan.Digest().String(),
		generation.Path(), generation.Version(), generation.TeamID(),
		generation.SigningIdentifier(), generation.DesignatedRequirement(),
		generation.CDHash(), generation.BundleDigest().String(),
		generation.EntitlementsDigest().String(), generation.Device(),
		generation.Inode(), h.policy.String(),
	}
	values = append(values, details...)
	for _, value := range values {
		var size [8]byte
		binary.BigEndian.PutUint64(size[:], uint64(len(value)))
		_, _ = digest.Write(size[:])
		_, _ = digest.Write([]byte(value))
	}
	var result deployment.SHA256
	copy(result[:], digest.Sum(nil))
	return result
}
