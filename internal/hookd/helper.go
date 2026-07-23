package hookd

import (
	"context"
	"encoding/json"
	"errors"
	"net/url"
)

const helperNotificationCapacity = 64

type helperNotification struct {
	Kind     string  `json:"kind"`
	Title    string  `json:"title"`
	Subtitle *string `json:"subtitle,omitempty"`
	Body     *string `json:"body,omitempty"`
	URL      *string `json:"url,omitempty"`
	Repo     *string `json:"repo,omitempty"`
}

type helperReply struct {
	OK      bool    `json:"ok"`
	Version *string `json:"version,omitempty"`
	Error   *string `json:"error,omitempty"`
}

type notificationHub struct {
	pending chan json.RawMessage
}

func newNotificationHub() *notificationHub {
	return &notificationHub{pending: make(chan json.RawMessage, helperNotificationCapacity)}
}

func (h *notificationHub) publish(ctx context.Context, payload json.RawMessage) error {
	select {
	case h.pending <- append(json.RawMessage(nil), payload...):
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (h *notificationHub) next(ctx context.Context) (json.RawMessage, error) {
	select {
	case payload := <-h.pending:
		return append(json.RawMessage(nil), payload...), nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func decodeHelperNotification(payload []byte) (helperNotification, json.RawMessage, error) {
	var request helperNotification
	if err := decodeStrict(payload, &request); err != nil {
		return helperNotification{}, nil, errors.New("captain: invalid helper notification")
	}
	if request.Kind == "" || request.Title == "" {
		return helperNotification{}, nil, errors.New("captain: helper notification kind and title are required")
	}
	if request.URL != nil {
		parsed, err := url.Parse(*request.URL)
		if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") {
			return helperNotification{}, nil, errors.New("captain: helper notification URL must be HTTP or HTTPS")
		}
	}
	canonical, err := json.Marshal(request)
	if err != nil {
		return helperNotification{}, nil, err
	}
	return request, canonical, nil
}
