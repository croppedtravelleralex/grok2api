package web

import (
	"testing"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
)

func TestSSOBuildAffinityUsesWebAccountID(t *testing.T) {
	credential := accountdomain.Credential{ID: 99, SourceKey: "sso:should-not-control-egress"}
	if got := ssoBuildAffinity(credential); got != "99" {
		t.Fatalf("affinity=%q want=99", got)
	}
}
