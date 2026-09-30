package hookd

import "testing"

func TestHostTrustSignsControlAndAdmitsTheSameUserToBusiness(t *testing.T) {
	t.Parallel()
	trust := hostTrust()
	if trust.Control == nil || trust.Control.Digest() != hostRequirement().Digest() {
		t.Fatalf("control lane requirement = %#v", trust.Control)
	}
	if trust.Business != nil {
		t.Fatalf("business lane = %#v, want the same-user floor alone", trust.Business)
	}
}
