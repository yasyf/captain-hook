package wireproto

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"fmt"
	"math"
	"strconv"
	"strings"
	"time"
)

//go:embed resource.json
var resourceDefinition []byte

type resourceSpec struct {
	EnvPrefix string           `json:"env_prefix"`
	Defaults  resourceDefaults `json:"defaults"`
}

type resourceDefaults struct {
	Enabled               bool    `json:"enabled"`
	SampleIntervalSeconds int     `json:"sample_interval_seconds"`
	MinRuntimeSeconds     int     `json:"min_runtime_seconds"`
	CPUFraction           float64 `json:"cpu_fraction"`
	SustainSeconds        int     `json:"sustain_seconds"`
	DiskBytesPerSecond    uint64  `json:"disk_bytes_per_second"`
	GraceSeconds          int     `json:"grace_seconds"`
	EscalateAfterSeconds  int     `json:"escalate_after_seconds"`
	MaxTrackedPerSession  int     `json:"max_tracked_per_session"`
	RegistryCap           int     `json:"registry_cap"`
}

var resource = loadResource()

func loadResource() resourceSpec {
	var spec resourceSpec
	decoder := json.NewDecoder(bytes.NewReader(resourceDefinition))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&spec); err != nil {
		panic(fmt.Sprintf("captain: resource definition: %v", err))
	}
	return spec
}

// ResourceSettings is the host-side configuration of the subprocess resource
// monitor: how often it samples, what counts as a sustained hog, how long it
// waits between the warning and the judge, and the bounds on what it tracks.
type ResourceSettings struct {
	Enabled              bool
	SampleInterval       time.Duration
	MinRuntime           time.Duration
	Sustain              time.Duration
	Grace                time.Duration
	EscalateAfter        time.Duration
	CPUFraction          float64
	DiskBytesPerSecond   uint64
	MaxTrackedPerSession int
	RegistryCap          int
}

// ParseResourceSettings reads the embedded defaults and applies every
// HOOKS_PERFORMANCE_<KEY> override env carries. A value that does not parse as
// its key's type is an error, never a silent default.
func ParseResourceSettings(env map[string]string) (ResourceSettings, error) {
	overrides := resourceOverrides{env: env}
	defaults := resource.Defaults
	settings := ResourceSettings{
		Enabled:              overrides.flag("enabled", defaults.Enabled),
		SampleInterval:       overrides.seconds("sample_interval_seconds", defaults.SampleIntervalSeconds),
		MinRuntime:           overrides.seconds("min_runtime_seconds", defaults.MinRuntimeSeconds),
		Sustain:              overrides.seconds("sustain_seconds", defaults.SustainSeconds),
		Grace:                overrides.seconds("grace_seconds", defaults.GraceSeconds),
		EscalateAfter:        overrides.seconds("escalate_after_seconds", defaults.EscalateAfterSeconds),
		CPUFraction:          overrides.fraction("cpu_fraction", defaults.CPUFraction),
		DiskBytesPerSecond:   overrides.bytes("disk_bytes_per_second", defaults.DiskBytesPerSecond),
		MaxTrackedPerSession: overrides.count("max_tracked_per_session", defaults.MaxTrackedPerSession),
		RegistryCap:          overrides.count("registry_cap", defaults.RegistryCap),
	}
	if overrides.err != nil {
		return ResourceSettings{}, overrides.err
	}
	if err := settings.validate(); err != nil {
		return ResourceSettings{}, err
	}
	return settings, nil
}

const maxResourceSeconds = 86_400

func (s ResourceSettings) validate() error {
	durations := map[string]time.Duration{
		"SAMPLE_INTERVAL_SECONDS": s.SampleInterval, "MIN_RUNTIME_SECONDS": s.MinRuntime,
		"SUSTAIN_SECONDS": s.Sustain, "GRACE_SECONDS": s.Grace,
	}
	for key, value := range durations {
		if value <= 0 || value > maxResourceSeconds*time.Second {
			return fmt.Errorf("captain: %s%s must be between 1 and %d", resource.EnvPrefix, key, maxResourceSeconds)
		}
	}
	switch {
	case s.EscalateAfter < 0 || s.EscalateAfter > maxResourceSeconds*time.Second:
		return fmt.Errorf("captain: %sESCALATE_AFTER_SECONDS must be between 0 and %d", resource.EnvPrefix, maxResourceSeconds)
	case math.IsNaN(s.CPUFraction) || math.IsInf(s.CPUFraction, 0) || s.CPUFraction <= 0 || s.CPUFraction > 1:
		return fmt.Errorf("captain: %sCPU_FRACTION must be a finite share in (0, 1]", resource.EnvPrefix)
	case s.MaxTrackedPerSession < 1:
		return fmt.Errorf("captain: %sMAX_TRACKED_PER_SESSION must be at least 1", resource.EnvPrefix)
	case s.RegistryCap < 1:
		return fmt.Errorf("captain: %sREGISTRY_CAP must be at least 1", resource.EnvPrefix)
	}
	return nil
}

type resourceOverrides struct {
	env map[string]string
	err error
}

func (o *resourceOverrides) lookup(key string) (string, string, bool) {
	name := resource.EnvPrefix + strings.ToUpper(key)
	value, ok := o.env[name]
	return name, value, ok
}

func (o *resourceOverrides) fail(name, value string, err error) {
	if o.err == nil {
		o.err = fmt.Errorf("captain: %s=%q: %w", name, value, err)
	}
}

func (o *resourceOverrides) flag(key string, fallback bool) bool {
	name, value, ok := o.lookup(key)
	if !ok {
		return fallback
	}
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		o.fail(name, value, err)
		return fallback
	}
	return parsed
}

func (o *resourceOverrides) count(key string, fallback int) int {
	name, value, ok := o.lookup(key)
	if !ok {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		o.fail(name, value, err)
		return fallback
	}
	return parsed
}

func (o *resourceOverrides) seconds(key string, fallback int) time.Duration {
	seconds := o.count(key, fallback)
	if seconds < -maxResourceSeconds || seconds > maxResourceSeconds {
		o.fail(resource.EnvPrefix+strings.ToUpper(key), strconv.Itoa(seconds), fmt.Errorf("outside ±%d seconds", maxResourceSeconds))
		return time.Duration(fallback) * time.Second
	}
	return time.Duration(seconds) * time.Second
}

func (o *resourceOverrides) fraction(key string, fallback float64) float64 {
	name, value, ok := o.lookup(key)
	if !ok {
		return fallback
	}
	parsed, err := strconv.ParseFloat(value, 64)
	if err != nil {
		o.fail(name, value, err)
		return fallback
	}
	return parsed
}

func (o *resourceOverrides) bytes(key string, fallback uint64) uint64 {
	name, value, ok := o.lookup(key)
	if !ok {
		return fallback
	}
	parsed, err := strconv.ParseUint(value, 10, 64)
	if err != nil {
		o.fail(name, value, err)
		return fallback
	}
	return parsed
}
