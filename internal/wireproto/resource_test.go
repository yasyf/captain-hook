package wireproto

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"
)

func TestResourceSettingsDefaultsEqualTheDefinition(t *testing.T) {
	t.Parallel()
	raw, err := os.ReadFile("resource.json")
	if err != nil {
		t.Fatal(err)
	}
	var definition struct {
		EnvPrefix string `json:"env_prefix"`
		Defaults  struct {
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
		} `json:"defaults"`
	}
	if err := json.Unmarshal(raw, &definition); err != nil {
		t.Fatal(err)
	}
	if definition.EnvPrefix != "HOOKS_PERFORMANCE_" {
		t.Fatalf("env prefix = %q", definition.EnvPrefix)
	}
	settings, err := ParseResourceSettings(nil)
	if err != nil {
		t.Fatal(err)
	}
	want := definition.Defaults
	got := ResourceSettings{
		Enabled:              want.Enabled,
		SampleInterval:       time.Duration(want.SampleIntervalSeconds) * time.Second,
		MinRuntime:           time.Duration(want.MinRuntimeSeconds) * time.Second,
		Sustain:              time.Duration(want.SustainSeconds) * time.Second,
		Grace:                time.Duration(want.GraceSeconds) * time.Second,
		EscalateAfter:        time.Duration(want.EscalateAfterSeconds) * time.Second,
		CPUFraction:          want.CPUFraction,
		DiskBytesPerSecond:   want.DiskBytesPerSecond,
		MaxTrackedPerSession: want.MaxTrackedPerSession,
		RegistryCap:          want.RegistryCap,
	}
	if settings != got {
		t.Fatalf("ParseResourceSettings(nil) = %+v, want the definition %+v", settings, got)
	}
	if !settings.Enabled || settings.SampleInterval != 15*time.Second || settings.CPUFraction != 0.5 {
		t.Fatalf("definition drifted from the plan: %+v", settings)
	}
}

func TestResourceSettingsApplyEveryOverride(t *testing.T) {
	t.Parallel()
	settings, err := ParseResourceSettings(map[string]string{
		"HOOKS_PERFORMANCE_ENABLED":                 "false",
		"HOOKS_PERFORMANCE_SAMPLE_INTERVAL_SECONDS": "5",
		"HOOKS_PERFORMANCE_MIN_RUNTIME_SECONDS":     "30",
		"HOOKS_PERFORMANCE_CPU_FRACTION":            "0.25",
		"HOOKS_PERFORMANCE_SUSTAIN_SECONDS":         "20",
		"HOOKS_PERFORMANCE_DISK_BYTES_PER_SECOND":   "1024",
		"HOOKS_PERFORMANCE_GRACE_SECONDS":           "10",
		"HOOKS_PERFORMANCE_ESCALATE_AFTER_SECONDS":  "0",
		"HOOKS_PERFORMANCE_MAX_TRACKED_PER_SESSION": "4",
		"HOOKS_PERFORMANCE_REGISTRY_CAP":            "2",
		"HOOKS_PROFILE":                             "strict",
	})
	if err != nil {
		t.Fatal(err)
	}
	want := ResourceSettings{
		Enabled: false, SampleInterval: 5 * time.Second, MinRuntime: 30 * time.Second, Sustain: 20 * time.Second,
		Grace: 10 * time.Second, EscalateAfter: 0, CPUFraction: 0.25, DiskBytesPerSecond: 1024,
		MaxTrackedPerSession: 4, RegistryCap: 2,
	}
	if settings != want {
		t.Fatalf("overridden settings = %+v, want %+v", settings, want)
	}
}

func TestResourceSettingsRefuseABadValue(t *testing.T) {
	t.Parallel()
	for name, env := range map[string]map[string]string{
		"bool":     {"HOOKS_PERFORMANCE_ENABLED": "sometimes"},
		"int":      {"HOOKS_PERFORMANCE_GRACE_SECONDS": "1m"},
		"float":    {"HOOKS_PERFORMANCE_CPU_FRACTION": "half"},
		"bytes":    {"HOOKS_PERFORMANCE_DISK_BYTES_PER_SECOND": "-1"},
		"interval": {"HOOKS_PERFORMANCE_SAMPLE_INTERVAL_SECONDS": "0"},

		"negative grace":       {"HOOKS_PERFORMANCE_GRACE_SECONDS": "-5"},
		"sustain over a day":   {"HOOKS_PERFORMANCE_SUSTAIN_SECONDS": "86401"},
		"overflowing seconds":  {"HOOKS_PERFORMANCE_MIN_RUNTIME_SECONDS": "9223372036854775807"},
		"negative escalation":  {"HOOKS_PERFORMANCE_ESCALATE_AFTER_SECONDS": "-1"},
		"zero cpu fraction":    {"HOOKS_PERFORMANCE_CPU_FRACTION": "0"},
		"cpu fraction above 1": {"HOOKS_PERFORMANCE_CPU_FRACTION": "1.5"},
		"nan cpu fraction":     {"HOOKS_PERFORMANCE_CPU_FRACTION": "NaN"},
		"inf cpu fraction":     {"HOOKS_PERFORMANCE_CPU_FRACTION": "+Inf"},
		"zero tracked":         {"HOOKS_PERFORMANCE_MAX_TRACKED_PER_SESSION": "0"},
		"zero registry cap":    {"HOOKS_PERFORMANCE_REGISTRY_CAP": "0"},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			if _, err := ParseResourceSettings(env); err == nil || !strings.Contains(err.Error(), "HOOKS_PERFORMANCE_") {
				t.Fatalf("ParseResourceSettings(%v) error = %v, want one naming the key", env, err)
			}
		})
	}
}
