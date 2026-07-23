package account

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
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
	payload := []byte(`{"model":"grok-imagine-image","messages":[{"role":"user","content":"minimal probe"}],"image_config":{"n":1},"stream":false}`)
	response, err := adapter.ForwardResponse(ctx, provider.ResponseResourceRequest{
		Credential: candidate, Method: http.MethodPost, Path: "/chat/completions", Body: payload,
		Model: "grok-imagine-image", NormalizeBody: false, Operation: "chat",
	})
	if err != nil {
		probeErr := fmt.Errorf("连接失败: %w", err)
		s.recordImagineProbeResult(ctx, candidate, probeErr)
		return probeErr
	}
	defer response.Body.Close()
	body, readErr := io.ReadAll(io.LimitReader(response.Body, 64<<10))
	if readErr != nil {
		probeErr := fmt.Errorf("读取响应: %w", readErr)
		s.recordImagineProbeResult(ctx, candidate, probeErr)
		return probeErr
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		snippet := strings.TrimSpace(string(body))
		if len(snippet) > 200 {
			snippet = snippet[:200]
		}
		probeErr := fmt.Errorf("status %d: %s", response.StatusCode, snippet)
		s.recordImagineProbeResult(ctx, candidate, probeErr)
		return probeErr
	}
	var envelope struct {
		Model string `json:"model"`
	}
	_ = json.Unmarshal(body, &envelope)
	s.recordImagineProbeResult(ctx, candidate, nil)
	return nil
}

func (s *Service) recordImagineProbeResult(ctx context.Context, candidate accountdomain.Credential, probeErr error) {
	now := s.now()
	if probeErr == nil {
		_ = s.accounts.SaveModelState(ctx, accountdomain.ModelState{
			AccountID: candidate.ID, UpstreamModel: imagineUpstream, Status: accountdomain.ModelStatusAvailable,
			Reason: "probe_lite_ok", LastAttemptAt: now, LastSuccessAt: &now, UpdatedAt: now,
		})
		s.QueueQuotaRefresh(candidate.ID, "imagine")
		return
	}
	reason := strings.TrimSpace(probeErr.Error())
	if len(reason) > 240 {
		reason = reason[:240]
	}
	_ = s.accounts.SaveModelState(ctx, accountdomain.ModelState{
		AccountID: candidate.ID, UpstreamModel: imagineUpstream, Status: modelStatusFromProbeError(probeErr),
		Reason: reason, LastAttemptAt: now, UpdatedAt: now,
	})
}

func modelStatusFromProbeError(err error) accountdomain.ModelStatus {
	msg := strings.ToLower(err.Error())
	if strings.Contains(msg, "unauthorized") || strings.Contains(msg, "credential") {
		return accountdomain.ModelStatusAuthFailed
	}
	if idx := strings.Index(msg, "status "); idx >= 0 {
		rest := msg[idx+len("status "):]
		codeText, _, _ := strings.Cut(rest, ":")
		if code, parseErr := strconv.Atoi(strings.TrimSpace(codeText)); parseErr == nil {
			switch {
			case code == http.StatusTooManyRequests:
				return accountdomain.ModelStatusQuotaExhausted
			case code == http.StatusUnauthorized || code == http.StatusForbidden:
				return accountdomain.ModelStatusAuthFailed
			case code >= 400 && code < 500:
				return accountdomain.ModelStatusSignatureFailed
			}
		}
	}
	return accountdomain.ModelStatusUnknown
}
