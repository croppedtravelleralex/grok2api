package account

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/infra/provider"
)

func (s *Service) probeWebQuotaL0(ctx context.Context, candidate accountdomain.Credential, lane WebLane) error {
	ready, err := s.ensureCredential(ctx, candidate, false, false, false)
	if err != nil {
		return err
	}
	switch lane {
	case WebLaneImage:
		if _, err := s.RefreshQuotaMode(ctx, ready.ID, "imagine"); err != nil {
			return err
		}
	case WebLaneChat:
		if _, err := s.RefreshQuotaMode(ctx, ready.ID, "fast"); err != nil {
			return err
		}
		if _, err := s.RefreshQuotaMode(ctx, ready.ID, "auto"); err != nil {
			return err
		}
	}
	return nil
}

func (s *Service) probeWebChatL1(ctx context.Context, candidate accountdomain.Credential) error {
	if s.providers == nil {
		return fmt.Errorf("Grok Web Provider 未注册")
	}
	adapter, ok := s.providers.Responses(accountdomain.ProviderWeb)
	if !ok {
		return fmt.Errorf("Grok Web Responses Provider 未注册")
	}
	payload := []byte(`{"model":"grok-3-fast","input":"Reply OK only.","max_output_tokens":16,"store":false,"stream":false}`)
	response, err := adapter.ForwardResponse(ctx, provider.ResponseResourceRequest{
		Credential: candidate, Method: http.MethodPost, Path: "/responses", Body: payload,
		Model: "grok-3-fast", NormalizeBody: false, Operation: "responses",
	})
	if err != nil {
		return fmt.Errorf("连接失败: %w", err)
	}
	defer response.Body.Close()
	body, readErr := io.ReadAll(io.LimitReader(response.Body, 64<<10))
	if readErr != nil {
		return fmt.Errorf("读取响应: %w", readErr)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		snippet := strings.TrimSpace(string(body))
		if len(snippet) > 200 {
			snippet = snippet[:200]
		}
		return fmt.Errorf("status %d: %s", response.StatusCode, snippet)
	}
	return nil
}

func (s *Service) probeWebLiteL2(ctx context.Context, candidate accountdomain.Credential) error {
	if s.providers == nil {
		return fmt.Errorf("Grok Web Provider 未注册")
	}
	adapter, ok := s.providers.Responses(accountdomain.ProviderWeb)
	if !ok {
		return fmt.Errorf("Grok Web Responses Provider 未注册")
	}
	payload := []byte(`{"model":"grok-imagine-image","input":"minimal probe","max_output_tokens":16,"store":false,"stream":false}`)
	response, err := adapter.ForwardResponse(ctx, provider.ResponseResourceRequest{
		Credential: candidate, Method: http.MethodPost, Path: "/chat/completions", Body: payload,
		Model: "grok-imagine-image", NormalizeBody: false, Operation: "chat",
	})
	if err != nil {
		return fmt.Errorf("连接失败: %w", err)
	}
	defer response.Body.Close()
	body, readErr := io.ReadAll(io.LimitReader(response.Body, 64<<10))
	if readErr != nil {
		return fmt.Errorf("读取响应: %w", readErr)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		snippet := strings.TrimSpace(string(body))
		if len(snippet) > 200 {
			snippet = snippet[:200]
		}
		return fmt.Errorf("status %d: %s", response.StatusCode, snippet)
	}
	var envelope struct {
		Model string `json:"model"`
	}
	_ = json.Unmarshal(body, &envelope)
	now := s.now()
	_ = s.accounts.SaveModelState(ctx, accountdomain.ModelState{
		AccountID: candidate.ID, UpstreamModel: imagineUpstream, Status: accountdomain.ModelStatusAvailable,
		Reason: "probe_lite_ok", LastAttemptAt: now, LastSuccessAt: &now, UpdatedAt: now,
	})
	s.QueueQuotaRefresh(candidate.ID, "imagine")
	return nil
}
