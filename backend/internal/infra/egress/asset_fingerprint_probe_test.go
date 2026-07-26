package egress

import (
	"io"
	"os"
	"testing"

	fhttp "github.com/bogdanfinn/fhttp"
	tlsclient "github.com/bogdanfinn/tls-client"
	"github.com/bogdanfinn/tls-client/profiles"
)

// 诊断用探针：定位 assets.grok.com 对 tls-client 返回 403 而 curl_cffi 返回 200 的差异。
// 默认跳过；需要真实出口与凭据时用环境变量运行：
//
//	GROK_PROBE_PROXY=... GROK_PROBE_SSO=... GROK_PROBE_UA=... GROK_PROBE_URL=... \
//	  go test ./internal/infra/egress/ -run TestAssetDownloadFingerprint -v
//
// Chrome 拉取图片子资源时的真实请求头顺序（HTTP/2 小写）。
var chromeImageHeaderOrder = []string{
	"sec-ch-ua",
	"sec-ch-ua-mobile",
	"sec-ch-ua-platform",
	"user-agent",
	"accept",
	"sec-fetch-site",
	"sec-fetch-mode",
	"sec-fetch-dest",
	"referer",
	"accept-encoding",
	"accept-language",
	"cookie",
}

var chromePseudoHeaderOrder = []string{":method", ":authority", ":scheme", ":path"}

type probeVariant struct {
	name        string
	profile     profiles.ClientProfile
	headerOrder bool
	pseudoOrder bool
	fullHeaders bool
	dropOrigin  bool
}

func TestAssetDownloadFingerprint(t *testing.T) {
	proxy := os.Getenv("GROK_PROBE_PROXY")
	sso := os.Getenv("GROK_PROBE_SSO")
	target := os.Getenv("GROK_PROBE_URL")
	userAgent := os.Getenv("GROK_PROBE_UA")
	if sso == "" || target == "" || userAgent == "" {
		t.Skip("需要 GROK_PROBE_SSO / GROK_PROBE_URL / GROK_PROBE_UA（GROK_PROBE_PROXY 可空表示直连）")
	}
	cookie := "sso=" + sso + "; sso-rw=" + sso

	variants := []probeVariant{
		{name: "baseline(生产现状:146,无顺序,6头,带Origin)", profile: profiles.Chrome_146},
		{name: "146+header顺序", profile: profiles.Chrome_146, headerOrder: true},
		{name: "146+header+伪头顺序", profile: profiles.Chrome_146, headerOrder: true, pseudoOrder: true},
		{name: "146+完整Chrome头(去Origin)", profile: profiles.Chrome_146, fullHeaders: true, dropOrigin: true},
		{name: "146+完整头+顺序(去Origin)", profile: profiles.Chrome_146, fullHeaders: true, dropOrigin: true, headerOrder: true, pseudoOrder: true},
		{name: "133+完整头+顺序(去Origin)", profile: profiles.Chrome_133, fullHeaders: true, dropOrigin: true, headerOrder: true, pseudoOrder: true},
		{name: "131+完整头+顺序(去Origin)", profile: profiles.Chrome_131, fullHeaders: true, dropOrigin: true, headerOrder: true, pseudoOrder: true},
		{name: "124+完整头+顺序(去Origin)", profile: profiles.Chrome_124, fullHeaders: true, dropOrigin: true, headerOrder: true, pseudoOrder: true},
		{name: "120+完整头+顺序(去Origin)", profile: profiles.Chrome_120, fullHeaders: true, dropOrigin: true, headerOrder: true, pseudoOrder: true},
	}

	for _, variant := range variants {
		status, size, err := runAssetProbe(variant, proxy, target, userAgent, cookie)
		switch {
		case err != nil:
			t.Logf("%-42s ERR  %v", variant.name, err)
		default:
			t.Logf("%-42s http=%d bytes=%d", variant.name, status, size)
		}
	}
}

func runAssetProbe(variant probeVariant, proxyURL, target, userAgent, cookie string) (int, int, error) {
	options := []tlsclient.HttpClientOption{
		tlsclient.WithTimeoutSeconds(60),
		tlsclient.WithClientProfile(variant.profile),
		tlsclient.WithNotFollowRedirects(),
	}
	if proxyURL != "" {
		options = append(options, tlsclient.WithProxyUrl(proxyURL))
	}
	client, err := tlsclient.NewHttpClient(tlsclient.NewNoopLogger(), options...)
	if err != nil {
		return 0, 0, err
	}
	defer client.CloseIdleConnections()

	request, err := fhttp.NewRequest(fhttp.MethodGet, target, nil)
	if err != nil {
		return 0, 0, err
	}
	request.Header.Set("Accept", "image/avif,image/webp,image/apng,image/*,*/*;q=0.8")
	request.Header.Set("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
	request.Header.Set("User-Agent", userAgent)
	if !variant.dropOrigin {
		request.Header.Set("Origin", "https://grok.com")
	}
	request.Header.Set("Referer", "https://grok.com/")
	request.Header.Set("Cookie", cookie)
	if variant.fullHeaders {
		request.Header.Set("sec-ch-ua", `"Chromium";v="146", "Google Chrome";v="146", "Not?A_Brand";v="24"`)
		request.Header.Set("sec-ch-ua-mobile", "?0")
		request.Header.Set("sec-ch-ua-platform", `"macOS"`)
		request.Header.Set("sec-fetch-site", "cross-site")
		request.Header.Set("sec-fetch-mode", "no-cors")
		request.Header.Set("sec-fetch-dest", "image")
		request.Header.Set("Accept-Encoding", "gzip, deflate, br, zstd")
	}
	if variant.headerOrder {
		request.Header[fhttp.HeaderOrderKey] = chromeImageHeaderOrder
	}
	if variant.pseudoOrder {
		request.Header[fhttp.PHeaderOrderKey] = chromePseudoHeaderOrder
	}

	response, err := client.Do(request)
	if err != nil {
		return 0, 0, err
	}
	defer response.Body.Close()
	body, _ := io.ReadAll(response.Body)
	return response.StatusCode, len(body), nil
}
