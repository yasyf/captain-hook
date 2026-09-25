package snapshots

import (
	"bytes"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"strconv"
	"sync"

	"github.com/santhosh-tekuri/jsonschema/v6"
)

const MaxFrameBytes = 1 << 20

//go:embed schema/*.json
var schemas embed.FS

var compiled = sync.OnceValues(func() (map[string]*jsonschema.Schema, error) {
	compiler := jsonschema.NewCompiler()
	names, err := schemas.ReadDir("schema")
	if err != nil {
		return nil, err
	}
	for _, name := range names {
		data, err := schemas.ReadFile("schema/" + name.Name())
		if err != nil {
			return nil, err
		}
		document, err := jsonschema.UnmarshalJSON(bytes.NewReader(data))
		if err != nil {
			return nil, err
		}
		if err := compiler.AddResource("https://captain.invalid/"+name.Name(), document); err != nil {
			return nil, err
		}
	}
	result := make(map[string]*jsonschema.Schema)
	for _, name := range names {
		schema, err := compiler.Compile("https://captain.invalid/" + name.Name())
		if err != nil {
			return nil, err
		}
		result[name.Name()] = schema
	}
	return result, nil
})

func Validate(name string, data json.RawMessage) error {
	if len(data) == 0 || len(data) > MaxFrameBytes {
		return fmt.Errorf("captain: snapshot %s size %d exceeds bounds", name, len(data))
	}
	validators, err := compiled()
	if err != nil {
		return err
	}
	value, err := jsonschema.UnmarshalJSON(bytes.NewReader(data))
	if err != nil {
		return err
	}
	value, err = boundedJSONNumbers(value)
	if err != nil {
		return err
	}
	schema, ok := validators[name+".schema.json"]
	if !ok {
		return fmt.Errorf("captain: unknown snapshot schema %q", name)
	}
	return schema.Validate(value)
}

var errNumberMagnitude = errors.New("captain: snapshot number exceeds finite binary64 magnitude")

func boundedJSONNumbers(value any) (any, error) {
	switch value := value.(type) {
	case json.Number:
		zero := true
		for _, digit := range value.String() {
			if digit == 'e' || digit == 'E' {
				break
			}
			if digit >= '1' && digit <= '9' {
				zero = false
				break
			}
		}
		if zero {
			return json.Number("0"), nil
		}
		if number, err := strconv.ParseFloat(value.String(), 64); err != nil || number == 0 {
			return nil, errNumberMagnitude
		}
	case []any:
		for i, item := range value {
			normalized, err := boundedJSONNumbers(item)
			if err != nil {
				return nil, err
			}
			value[i] = normalized
		}
	case map[string]any:
		for key, item := range value {
			normalized, err := boundedJSONNumbers(item)
			if err != nil {
				return nil, err
			}
			value[key] = normalized
		}
	}
	return value, nil
}

func DefaultConfig() (json.RawMessage, error) {
	data, err := schemas.ReadFile("schema/config.schema.json")
	if err != nil {
		return nil, err
	}
	var schema struct {
		Properties map[string]struct {
			Default json.RawMessage `json:"default"`
		} `json:"properties"`
	}
	if err := json.Unmarshal(data, &schema); err != nil {
		return nil, err
	}
	defaults := make(map[string]json.RawMessage, len(schema.Properties))
	for name, property := range schema.Properties {
		if len(property.Default) == 0 {
			return nil, fmt.Errorf("captain: snapshot config %s has no default", name)
		}
		defaults[name] = property.Default
	}
	return json.Marshal(defaults)
}

type Authority struct {
	Kind         string   `json:"kind"`
	EffectiveUID string   `json:"effective_uid"`
	Roots        []string `json:"roots,omitempty"`
}

type CallContext struct {
	Claimant           string    `json:"claimant"`
	Admission          string    `json:"admission"`
	Authority          Authority `json:"authority"`
	RegistryGeneration string    `json:"registry_generation"`
}

type RequestMetadata struct {
	Operation      string `json:"operation"`
	ID             string `json:"id"`
	DeadlineUnixMS int64  `json:"deadline_unix_ms"`
}

func Metadata(data json.RawMessage) (RequestMetadata, error) {
	if err := Validate("host-request", data); err != nil {
		return RequestMetadata{}, err
	}
	var wrapper struct {
		Request struct {
			Operation      string      `json:"operation"`
			ID             string      `json:"id"`
			DeadlineUnixMS json.Number `json:"deadline_unix_ms"`
		} `json:"request"`
	}
	if err := json.Unmarshal(data, &wrapper); err != nil {
		return RequestMetadata{}, err
	}
	metadata := RequestMetadata{Operation: wrapper.Request.Operation, ID: wrapper.Request.ID}
	if wrapper.Request.DeadlineUnixMS == "" {
		return metadata, nil
	}
	deadline, ok := new(big.Rat).SetString(wrapper.Request.DeadlineUnixMS.String())
	if !ok || !deadline.IsInt() || !deadline.Num().IsInt64() {
		return RequestMetadata{}, errors.New("captain: snapshot deadline is not an int64 integer")
	}
	metadata.DeadlineUnixMS = deadline.Num().Int64()
	return metadata, nil
}
