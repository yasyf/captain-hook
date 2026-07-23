package hookd

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestDecodeHelperNotificationCanonicalizesExactPayload(t *testing.T) {
	payload := []byte(`{"kind":"pr_open","title":"Block force-pushes","url":"https://example.com/pr/1"}`)
	request, canonical, err := decodeHelperNotification(payload)
	if err != nil {
		t.Fatal(err)
	}
	if request.Kind != "pr_open" || request.Title != "Block force-pushes" {
		t.Fatalf("request = %#v", request)
	}
	var roundTrip helperNotification
	if err := json.Unmarshal(canonical, &roundTrip); err != nil {
		t.Fatal(err)
	}
	if roundTrip.Kind != request.Kind || roundTrip.Title != request.Title {
		t.Fatalf("round trip = %#v", roundTrip)
	}
}

func TestDecodeHelperNotificationRejectsInvalidShape(t *testing.T) {
	for _, payload := range []string{
		`{}`,
		`{"kind":"pr_open"}`,
		`{"kind":"pr_open","title":"x","url":"file:///tmp/x"}`,
		`{"kind":"pr_open","title":"x","unknown":true}`,
	} {
		if _, _, err := decodeHelperNotification([]byte(payload)); err == nil {
			t.Fatalf("accepted %s", payload)
		}
	}
}

func TestNotificationHubPreservesOrderAndCopiesPayloads(t *testing.T) {
	hub := newNotificationHub()
	first := json.RawMessage(`{"kind":"one","title":"first"}`)
	if err := hub.publish(context.Background(), first); err != nil {
		t.Fatal(err)
	}
	first[2] = 'X'
	if err := hub.publish(context.Background(), json.RawMessage(`{"kind":"two","title":"second"}`)); err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"one", "two"} {
		payload, err := hub.next(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(string(payload), `"kind":"`+want+`"`) {
			t.Fatalf("payload = %s, want kind %q", payload, want)
		}
	}
}

func TestNotificationHubWaitsAreCancellable(t *testing.T) {
	hub := newNotificationHub()
	ctx, cancel := context.WithTimeout(context.Background(), time.Millisecond)
	defer cancel()
	if _, err := hub.next(ctx); err == nil {
		t.Fatal("next unexpectedly succeeded")
	}
}
