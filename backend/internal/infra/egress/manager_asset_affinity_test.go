package egress

import (
	"context"
	"testing"

	"github.com/chenyme/grok2api/backend/internal/domain/egress"
	"github.com/chenyme/grok2api/backend/internal/infra/security"
)

// 禁用最低 ID 的 asset 节点后，affinity 应落到剩余住宅节点而非静默失败。
func TestWebAssetAffinityUsesRemainingNodesWhenLowestDisabled(t *testing.T) {
	cipher, err := security.NewCipher("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
	if err != nil {
		t.Fatal(err)
	}
	nodes := []egress.Node{
		{ID: 111, Name: "udeal-asset", Scope: egress.ScopeWebAsset, Enabled: false, Health: 0.05},
		{ID: 201, Name: "wsres-asset-001", Scope: egress.ScopeWebAsset, Enabled: true, Health: 1},
		{ID: 202, Name: "wsres-asset-002", Scope: egress.ScopeWebAsset, Enabled: true, Health: 1},
	}
	manager := NewManager(egressRepositoryTestStub{nodes: nodes}, cipher)
	lease, err := manager.Acquire(context.Background(), egress.ScopeWebAsset, "42")
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Release()
	if lease.NodeID == 111 {
		t.Fatalf("selected disabled node 111")
	}
	if lease.NodeID != 201 && lease.NodeID != 202 {
		t.Fatalf("node = %d, want residential asset node 201 or 202", lease.NodeID)
	}
}
