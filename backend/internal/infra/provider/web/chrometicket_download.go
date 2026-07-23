package web

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"

	egressapp "github.com/chenyme/grok2api/backend/internal/application/egress"
	imagepipelineapp "github.com/chenyme/grok2api/backend/internal/application/imagepipeline"
	chrometicketdomain "github.com/chenyme/grok2api/backend/internal/domain/chrometicket"
	"github.com/chenyme/grok2api/backend/internal/domain/account"
	domainegress "github.com/chenyme/grok2api/backend/internal/domain/egress"
	infraegress "github.com/chenyme/grok2api/backend/internal/infra/egress"
)

type chromeDownloadState struct {
	Cookie    string
	UserAgent string
}

func chromeDownloadStateFromContext(ctx context.Context) chromeDownloadState {
	if run := imagepipelineapp.RunFromContext(ctx); run != nil {
		art := run.Artifacts()
		if cookie := strings.TrimSpace(art.ChromeDownloadCookie); cookie != "" {
			return chromeDownloadState{Cookie: cookie, UserAgent: strings.TrimSpace(art.ChromeUserAgent)}
		}
	}
	return chromeDownloadState{}
}

func sanitizeChromeTicketCookie(token, deviceCookie, egressCF string) string {
	token = normalizeSSOTokenValue(token)
	parts := []string{"sso=" + token, "sso-rw=" + token}
	if cf := egressapp.SanitizeCloudflareCookies(egressCF); cf != "" {
		parts = append(parts, cf)
	}
	deviceCookie = strings.TrimSpace(strings.Trim(deviceCookie, ";"))
	if deviceCookie == "" {
		return strings.Join(parts, "; ")
	}
	keep := make([]string, 0, 8)
	for part := range strings.SplitSeq(deviceCookie, ";") {
		part = strings.TrimSpace(part)
		if part == "" || !strings.Contains(part, "=") {
			continue
		}
		name := strings.ToLower(strings.TrimSpace(strings.Split(part, "=")[0]))
		if name == "sso" || name == "sso-rw" || name == "cf_clearance" || name == "__cf_bm" || name == "_cfuvid" || strings.HasPrefix(name, "cf_chl") {
			continue
		}
		keep = append(keep, part)
	}
	if len(keep) > 0 {
		parts = append(parts, strings.Join(keep, "; "))
	}
	return strings.Join(parts, "; ")
}

func mergeChromeTicketDownloadCookie(token, deviceCookie, egressCF string) string {
	cookie := sanitizeChromeTicketCookie(token, deviceCookie, egressCF)
	if strings.Contains(strings.ToLower(cookie), "cf_clearance=") {
		return cookie
	}
	deviceCookie = strings.TrimSpace(strings.Trim(deviceCookie, ";"))
	for part := range strings.SplitSeq(deviceCookie, ";") {
		part = strings.TrimSpace(part)
		if part == "" || !strings.Contains(part, "=") {
			continue
		}
		name := strings.ToLower(strings.TrimSpace(strings.Split(part, "=")[0]))
		if name == "cf_clearance" || name == "__cf_bm" || name == "_cfuvid" || strings.HasPrefix(name, "cf_chl") {
			return cookie + "; " + part
		}
	}
	return cookie
}

func normalizeSSOTokenValue(token string) string {
	return normalizeSSOToken(token)
}

func appendCloudflareCookie(base, warmed string) string {
	base = strings.TrimSpace(base)
	warmed = egressapp.SanitizeCloudflareCookies(warmed)
	if warmed == "" {
		return base
	}
	if strings.Contains(strings.ToLower(base), "cf_clearance=") {
		return base
	}
	if base == "" {
		return warmed
	}
	return base + "; " + warmed
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

func (a *Adapter) prepareChromeTicketDownloadCookie(ctx context.Context, credential account.Credential) {
	deviceCookie := chromeTicketCookieFromContext(ctx)
	if deviceCookie == "" || credential.ID == 0 {
		return
	}
	run := imagepipelineapp.RunFromContext(ctx)
	if run != nil && strings.TrimSpace(run.Artifacts().ChromeDownloadCookie) != "" {
		return
	}
	ticket, ok := chrometicketdomain.LeaseFromContext(ctx)
	userAgent := ""
	if ok {
		userAgent = strings.TrimSpace(ticket.UserAgent)
	}
	token, err := a.cipher.Decrypt(credential.EncryptedAccessToken)
	if err != nil {
		a.log().Warn("web_lite_asset_cf_warm_failed", "account_id", credential.ID, "error", err)
		return
	}
	warmed, usedUA, warmErr := a.warmChromeTicketSession(ctx, credential, token, deviceCookie, userAgent)
	if warmErr != nil {
		a.log().Warn("web_lite_asset_cf_warm_failed", "account_id", credential.ID, "error", warmErr)
		return
	}
	if run == nil {
		return
	}
	run.SetChromeDownloadCookie(warmed)
	if usedUA != "" {
		run.SetChromeUserAgent(usedUA)
	}
	a.log().Info("web_lite_asset_cf_warm", "account_id", credential.ID, "cf_set", strings.Contains(strings.ToLower(warmed), "cf_clearance="))
}

func (a *Adapter) warmChromeTicketSession(ctx context.Context, credential account.Credential, token, deviceCookie, preferredUA string) (string, string, error) {
	lease, err := a.egress.Acquire(ctx, domainegress.ScopeWeb, fmt.Sprintf("%d", credential.ID))
	if err != nil {
		return "", "", err
	}
	defer lease.Release()
	userAgent := strings.TrimSpace(preferredUA)
	if userAgent == "" {
		userAgent = lease.UserAgent
	}
	baseCookie := sanitizeChromeTicketCookie(token, deviceCookie, lease.CFCookies)
	cfg := a.config()
	warmURL := cfg.BaseURL + "/"
	headers := http.Header{}
	headers.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	headers.Set("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
	headers.Set("User-Agent", userAgent)
	headers.Set("Cookie", baseCookie)
	response, headerCF, err := warmGETWithRedirects(ctx, lease.Do, warmURL, headers, 8)
	if err != nil {
		return "", "", err
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 1<<20))
	parsed, _ := url.Parse(warmURL)
	jarCF := lease.JarCloudflareCookies(parsed)
	warmed := appendCloudflareCookie(baseCookie, joinCookieParts(headerCF, jarCF))
	return warmed, userAgent, nil
}

func warmGETWithRedirects(ctx context.Context, do func(*http.Request) (*http.Response, error), start string, headers http.Header, maxHops int) (*http.Response, string, error) {
	current, err := url.Parse(start)
	if err != nil {
		return nil, "", err
	}
	collected := ""
	for hop := 0; hop < maxHops; hop++ {
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, current.String(), nil)
		if err != nil {
			return nil, collected, err
		}
		request.Header = headers.Clone()
		response, err := do(request)
		if err != nil {
			return nil, collected, err
		}
		collected = joinCookieParts(collected, collectCloudflareFromResponse(response.Header))
		if response.StatusCode >= 200 && response.StatusCode < 300 {
			return response, collected, nil
		}
		if response.StatusCode < 300 || response.StatusCode >= 400 {
			_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 1<<20))
			response.Body.Close()
			return nil, collected, fmt.Errorf("warm grok.com returned %d", response.StatusCode)
		}
		location := strings.TrimSpace(response.Header.Get("Location"))
		response.Body.Close()
		if location == "" {
			return nil, collected, fmt.Errorf("warm redirect missing location")
		}
		next, err := current.Parse(location)
		if err != nil {
			return nil, collected, err
		}
		if !next.IsAbs() {
			next = current.ResolveReference(next)
		}
		current = next
	}
	return nil, collected, fmt.Errorf("warm exceeded redirect limit")
}

func resolveAssetDownloadCookie(token, leaseCF, deviceCookie string, downloadState chromeDownloadState) string {
	if strings.TrimSpace(leaseCF) != "" && deviceCookie != "" {
		return mergeChromeTicketDownloadCookie(token, deviceCookie, leaseCF)
	}
	if downloadState.Cookie != "" {
		return downloadState.Cookie
	}
	if deviceCookie != "" {
		return mergeChromeTicketDownloadCookie(token, deviceCookie, leaseCF)
	}
	return infraegress.BuildSSOCookie(token, leaseCF)
}

func applyAssetDownloadHeaders(headers http.Header, cfg Config, userAgent, cookie string) {
	headers.Set("Accept", "image/avif,image/webp,image/apng,image/*,*/*;q=0.8")
	headers.Set("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
	headers.Set("User-Agent", userAgent)
	headers.Set("Origin", cfg.BaseURL)
	headers.Set("Referer", cfg.BaseURL+"/")
	headers.Set("Cookie", cookie)
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
