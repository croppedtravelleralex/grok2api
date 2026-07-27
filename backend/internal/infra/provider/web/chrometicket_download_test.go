package web

import (
	"strings"
	"testing"
)

func TestSanitizeChromeTicketCookieKeepsDeviceIdentity(t *testing.T) {
	device := "grok_device_id=dev123; x-anonuserid=anon; cf_clearance=stale; sso=drop"
	got := sanitizeChromeTicketCookie("token-value", device, "cf_clearance=egress")
	for _, expected := range []string{"sso=token-value", "sso-rw=token-value", "cf_clearance=egress", "grok_device_id=dev123", "x-anonuserid=anon"} {
		if !strings.Contains(got, expected) {
			t.Fatalf("cookie missing %q: %s", expected, got)
		}
	}
	for _, forbidden := range []string{"cf_clearance=stale", "sso=drop"} {
		if strings.Contains(got, forbidden) {
			t.Fatalf("cookie must not contain %q: %s", forbidden, got)
		}
	}
}

func TestMergeChromeTicketDownloadCookiePullsCFromDevice(t *testing.T) {
	device := "grok_device_id=dev; cf_clearance=device-cf; __cf_bm=bm"
	got := mergeChromeTicketDownloadCookie("token", device, "")
	if !strings.Contains(got, "cf_clearance=device-cf") || !strings.Contains(got, "grok_device_id=dev") {
		t.Fatalf("unexpected cookie: %s", got)
	}
}

func TestMergeChromeTicketDownloadCookiePrefersSanitizedCF(t *testing.T) {
	got := mergeChromeTicketDownloadCookie("token", "cf_clearance=device-cf", "cf_clearance=egress")
	if strings.Contains(got, "cf_clearance=device-cf") {
		t.Fatalf("device cf must be dropped when egress cf exists: %s", got)
	}
	if !strings.Contains(got, "cf_clearance=egress") {
		t.Fatalf("egress cf missing: %s", got)
	}
}

func TestResolveAssetDownloadCookiePrefersEgressCF(t *testing.T) {
	warmed := "sso=t; sso-rw=t; cf_clearance=warmed"
	state := chromeDownloadState{Cookie: warmed}
	got := resolveAssetDownloadCookie("t", "cf_clearance=asset", "grok_device_id=dev", state)
	if !strings.Contains(got, "cf_clearance=asset") || !strings.Contains(got, "grok_device_id=dev") {
		t.Fatalf("asset egress cf path = %s", got)
	}
	if strings.Contains(got, "cf_clearance=warmed") {
		t.Fatalf("must not reuse warmed cf when egress cf exists: %s", got)
	}
}

func TestResolveAssetDownloadCookieSkipsWarmedCFWithoutEgressCF(t *testing.T) {
	warmed := "sso=t; sso-rw=t; cf_clearance=warmed; grok_device_id=dev"
	device := "grok_device_id=dev; cf_clearance=device-cf; x-anonuserid=anon"
	state := chromeDownloadState{Cookie: warmed}
	got := resolveAssetDownloadCookie("t", "", device, state)
	if strings.Contains(got, "cf_clearance=") {
		t.Fatalf("must not send cf_clearance on residential asset egress: %s", got)
	}
	if !strings.Contains(got, "grok_device_id=dev") || !strings.Contains(got, "sso=t") {
		t.Fatalf("expected SSO + device identity only: %s", got)
	}
}

func TestResolveAssetDownloadCookieReusesSsoOnlyWarmedState(t *testing.T) {
	warmed := "sso=t; sso-rw=t; grok_device_id=dev"
	state := chromeDownloadState{Cookie: warmed}
	got := resolveAssetDownloadCookie("t", "", "", state)
	if got != warmed {
		t.Fatalf("expected SSO-only warmed cookie, got %s", got)
	}
}
