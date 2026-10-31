package snapshots

import (
	"encoding/json"
	"fmt"
	"testing"
)

func TestInteractionRequestMetadataPreservesTheStrictHostBoundary(t *testing.T) {
	handle := `{"owner_epoch":"owner","snapshot_id":"snapshot","generation":"generation","lease_id":"lease"}`
	classifier := `{"id":"native","version":"1"}`
	view := fmt.Sprintf(`{"handle":%s,"classifier":%s,"selectors":[],"attachments":[]}`, handle, classifier)
	limits := `{"max_read_bytes":1048576,"max_source_read_bytes":1048576,"max_events":1024,"max_items":256,"max_output_bytes":1048576,"max_discovery_entries":1024,"max_sources":1}`
	for _, test := range []struct {
		name, operation, fields string
		valid                   bool
	}{
		{"bundle", "acquire_views", fmt.Sprintf(`"handle":%s,"tail_events":2`, handle), true},
		{"zero tail", "acquire_views", fmt.Sprintf(`"handle":%s,"tail_events":0`, handle), false},
		{"oversized tail", "acquire_views", fmt.Sprintf(`"handle":%s,"tail_events":257`, handle), false},
		{"bundle priority forgery", "acquire_views", fmt.Sprintf(`"handle":%s,"tail_events":2,"priority":"hook"`, handle), false},
		{"classification", "classify_view", fmt.Sprintf(`"view":%s,"classifier":%s`, view, classifier), true},
		{"question keys", "query", fmt.Sprintf(`"view":%s,"query":{"kind":"interaction_keys","name":"AskUserQuestion"}`, view), true},
		{"empty question name", "query", fmt.Sprintf(`"view":%s,"query":{"kind":"interaction_keys","name":""}`, view), false},
		{"instruction words", "query", fmt.Sprintf(`"view":%s,"query":{"kind":"instruction_turns","order":"forward"}`, view), true},
		{"instruction words without order", "query", fmt.Sprintf(`"view":%s,"query":{"kind":"instruction_turns"}`, view), false},
	} {
		t.Run(test.name, func(t *testing.T) {
			request := json.RawMessage(fmt.Sprintf(`{"schema":"captain.transcript/1","tool_registry":[],"request":{"schema":"cc-transcript.snapshot/1","id":"interaction","operation":%q,"deadline_unix_ms":4102444800000,"limits":%s,%s}}`, test.operation, limits, test.fields))
			metadata, err := Metadata(request)
			if !test.valid {
				if err == nil {
					t.Fatal("invalid interaction request accepted")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if metadata.Operation != test.operation || metadata.ID != "interaction" || metadata.DeadlineUnixMS != 4102444800000 {
				t.Fatalf("unexpected metadata: %+v", metadata)
			}
		})
	}
}
