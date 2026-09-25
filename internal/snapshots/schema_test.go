package snapshots

import (
	"encoding/json"
	"errors"
	"fmt"
	"testing"
)

func TestNumberMagnitudeIsBoundedBeforeSchemaArithmetic(t *testing.T) {
	for _, number := range []string{"1e100000000", "1e-100000000", "-1e100000000", "-1e-100000000"} {
		t.Run(number, func(t *testing.T) {
			request := json.RawMessage(fmt.Sprintf(`{"schema":"captain.transcript/1","tool_registry":[],"request":{"schema":"cc-transcript.snapshot/1","operation":"stats","id":%s}}`, number))
			if err := Validate("host-request", request); !errors.Is(err, errNumberMagnitude) {
				t.Fatalf("expected magnitude refusal before schema arithmetic, got %v", err)
			}
		})
	}
	for _, number := range []string{"0e100000000", "-0.0e-100000000"} {
		t.Run(number, func(t *testing.T) {
			value, err := boundedJSONNumbers(map[string]any{"nested": []any{json.Number(number)}})
			if err != nil {
				t.Fatal(err)
			}
			encoded, err := json.Marshal(value)
			if err != nil || string(encoded) != `{"nested":[0]}` {
				t.Fatalf("value=%s error=%v", encoded, err)
			}
		})
	}
	value, err := boundedJSONNumbers(json.Number("9007199254740990.5"))
	if err != nil || value != json.Number("9007199254740990.5") {
		t.Fatalf("exact fractional value changed: %v, %v", value, err)
	}
}

func TestMetadataNumericDeadlines(t *testing.T) {
	tests := []struct {
		name, number string
		want         int64
		valid        bool
	}{
		{"integer", "1700000000000", 1700000000000, true},
		{"decimal", "1700000000000.0", 1700000000000, true},
		{"exponent", "1.7e12", 1700000000000, true},
		{"negative exponent", "17000000000000e-1", 1700000000000, true},
		{"maximum", "9007199254740991", 9007199254740991, true},
		{"exact maximum exponent", "90071992547409910e-1", 9007199254740991, true},
		{"fractional", "1700000000000.5", 0, false},
		{"fractional near maximum", "9007199254740990.5", 0, false},
		{"above maximum", "9007199254740992", 0, false},
		{"string", `"1700000000000"`, 0, false},
		{"not a number", "NaN", 0, false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			request := json.RawMessage(fmt.Sprintf(`{"schema":"captain.transcript/1","tool_registry":[],"request":{"schema":"cc-transcript.snapshot/1","operation":"acquire","id":"numeric","deadline_unix_ms":%s,"path":"/fixture.jsonl","classifier":{"id":"default","version":"1"},"limits":{"max_read_bytes":1,"max_events":1,"max_items":1,"max_output_bytes":1,"max_discovery_entries":1,"max_sources":1}}}`, test.number))
			metadata, err := Metadata(request)
			if !test.valid {
				if err == nil {
					t.Fatal("invalid deadline accepted")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if metadata.Operation != "acquire" || metadata.ID != "numeric" || metadata.DeadlineUnixMS != test.want {
				t.Fatalf("metadata=%+v want deadline=%d", metadata, test.want)
			}
		})
	}
	metadata, err := Metadata(json.RawMessage(`{"schema":"captain.transcript/1","tool_registry":[],"request":{"schema":"cc-transcript.snapshot/1","operation":"stats","id":"no-deadline"}}`))
	if err != nil || metadata.DeadlineUnixMS != 0 {
		t.Fatalf("metadata=%+v error=%v", metadata, err)
	}
}

func TestRequestSchemaStrictness(t *testing.T) {
	tests := []struct {
		name, request string
		valid         bool
	}{
		{"stats", `{"schema":"captain.transcript/1","tool_registry":[],"request":{"schema":"cc-transcript.snapshot/1","operation":"stats","id":"one"}}`, true},
		{"unknown schema", `{"schema":"captain.transcript/2","request":{"schema":"cc-transcript.snapshot/1","operation":"stats","id":"one"}}`, false},
		{"priority forgery", `{"schema":"captain.transcript/1","tool_registry":[],"request":{"schema":"cc-transcript.snapshot/1","operation":"stats","id":"one","priority":"hook"}}`, false},
		{"context forgery", `{"schema":"captain.transcript/1","tool_registry":[],"request":{"schema":"cc-transcript.snapshot/1","operation":"stats","id":"one"},"context":{"admission":"hook"}}`, false},
		{"unknown operation", `{"schema":"captain.transcript/1","tool_registry":[],"request":{"schema":"cc-transcript.snapshot/1","operation":"execute","id":"one"}}`, false},
		{"numeric id", `{"schema":"captain.transcript/1","tool_registry":[],"request":{"schema":"cc-transcript.snapshot/1","operation":"stats","id":1}}`, false},
		{"trailing json", `{"schema":"captain.transcript/1","tool_registry":[],"request":{"schema":"cc-transcript.snapshot/1","operation":"stats","id":"one"}} {}`, false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			err := Validate("host-request", json.RawMessage(test.request))
			if (err == nil) != test.valid {
				t.Fatalf("valid=%v error=%v", test.valid, err)
			}
		})
	}
}

func TestDefaultConfigurationPreservesHookReserves(t *testing.T) {
	data, err := DefaultConfig()
	if err != nil {
		t.Fatal(err)
	}
	if err := Validate("config", data); err != nil {
		t.Fatal(err)
	}
	var config map[string]int64
	if err := json.Unmarshal(data, &config); err != nil {
		t.Fatal(err)
	}
	for key, want := range map[string]int64{"reserved_hook_loads": 1, "reserved_hook_leases": 32, "reserved_hook_accounted_bytes": 512 << 20, "max_retained_bytes": 1 << 30} {
		if config[key] != want {
			t.Fatalf("%s=%d want %d", key, config[key], want)
		}
	}
}
