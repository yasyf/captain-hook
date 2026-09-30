package hookd

import "testing"

func TestHostTrustAdmitsTheSameUserOnEveryLane(t *testing.T) {
	t.Parallel()
	trust := hostTrust()
	if trust.Control != nil || trust.Business != nil {
		t.Fatalf("trust = %#v, want the same-user floor alone", trust)
	}
}
