package web

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"

	egressapp "github.com/chenyme/grok2api/backend/internal/application/egress"
	"github.com/chenyme/grok2api/backend/internal/domain/account"
	domainegress "github.com/chenyme/grok2api/backend/internal/domain/egress"
	infraegress "github.com/chenyme/grok2api/backend/internal/infra/egress"
)

func buildChromeTicketDownloadCookie(token, leaseCF, warmedCF, deviceCookie string) string {
	cf := egressapp.SanitizeCloudflareCookies(joinCookieParts(leaseCF, warmedCF))
	cookie := infraegress.BuildSSOCookie(token, cf)
	return mergeChromeTicketCookie(cookie, deviceCookie)
}

func joinCookieParts(parts ...string) string {
	kept := make([]string, 0, len(parts))
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part != "" {
			kept = append(kept, part)
		}
	}
	return strings.Join(kept, "; ")
}

func collectCloudflareFromResponse(header http.Header) string {
	if header == nil {
		return ""
	}
	parts := make([]string, 0, len(header.Values("Set-Cookie")))
	for _, cookie := range header.Values("Set-Cookie") {
		segment := strings.TrimSpace(strings.Split(cookie, ";")[0])
		if segment != "" {
			parts = append(parts, segment)
		}
	}
	return egressapp.SanitizeCloudflareCookies(strings.Join(parts, "; "))
}

func (a *Adapter) warmChromeTicketCloudflare(ctx context.Context, credential account.Credential, deviceCookie string) (string, error) {
	deviceCookie = strings.TrimSpace(deviceCookie)
	if deviceCookie == "" || credential.ID == 0 {
		return "", nil
	}
	token, err := a.cipher.Decrypt(credential.EncryptedAccessToken)
	if err != nil {
		return "", err
	}
	lease, err := a.egress.Acquire(ctx, domainegress.ScopeWeb, fmt.Sprintf("%d", credential.ID))
	if err != nil {
		return "", err
	}
	defer lease.Release()
	cfg := a.config()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, cfg.BaseURL+"/", nil)
	if err != nil {
		return "", err
	}
	request.Header = buildHeaders(token, lease, "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	request.Header.Set("Cookie", mergeChromeTicketCookie(request.Header.Get("Cookie"), deviceCookie))
	applyAppHeaders(request.Header, cfg.BaseURL, cfg.BaseURL+"/")
	request.Header.Set("Sec-Fetch-Dest", "document")
	request.Header.Set("Sec-Fetch-Mode", "navigate")
	request.Header.Set("Sec-Fetch-Site", "none")
	response, err := lease.Do(request)
	if err != nil {
		return "", err
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 1<<20))
	if response.StatusCode < 200 || response.StatusCode >= 400 {
		return "", fmt.Errorf("warm grok.com returned %d", response.StatusCode)
	}
	return collectCloudflareFromResponse(response.Header), nil
}
