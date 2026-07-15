package web

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	infraegress "github.com/chenyme/grok2api/backend/internal/infra/egress"
)

const browserBridgeBodyLimit = 192 << 20

type browserBridge struct {
	baseURL string
	key     string
	client  *http.Client
}

type browserBridgeRequest struct {
	SessionKey string      `json:"sessionKey"`
	URL        string      `json:"url"`
	Method     string      `json:"method"`
	Headers    http.Header `json:"headers,omitempty"`
	Body       string      `json:"body,omitempty"`
	Cookie     string      `json:"cookie,omitempty"`
	ProxyURL   string      `json:"proxyUrl,omitempty"`
	UserAgent  string      `json:"userAgent,omitempty"`
	Referer    string      `json:"referer,omitempty"`
	TimeoutMS  int64       `json:"timeoutMs"`
}

type browserBridgeResponse struct {
	Status  int         `json:"status"`
	Headers http.Header `json:"headers,omitempty"`
	Body    string      `json:"body,omitempty"`
	Error   string      `json:"error,omitempty"`
}

type browserBridgeWebSocketRequest struct {
	SessionKey string           `json:"sessionKey,omitempty"`
	URL        string           `json:"url"`
	Messages   []map[string]any `json:"messages,omitempty"`
	Cookie     string           `json:"cookie,omitempty"`
	ProxyURL   string           `json:"proxyUrl,omitempty"`
	UserAgent  string           `json:"userAgent,omitempty"`
	Referer    string           `json:"referer,omitempty"`
	TimeoutMS  int64            `json:"timeoutMs"`
	IdleMS     int64            `json:"idleMs,omitempty"`
	Expected   int              `json:"expected,omitempty"`
}

type browserBridgeWebSocketResponse struct {
	Frames []string `json:"frames,omitempty"`
	Error  string   `json:"error,omitempty"`
}

func newBrowserBridge(baseURL, key string) *browserBridge {
	baseURL = strings.TrimRight(strings.TrimSpace(baseURL), "/")
	if baseURL == "" {
		return nil
	}
	return &browserBridge{
		baseURL: baseURL,
		key:     strings.TrimSpace(key),
		client: &http.Client{CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		}},
	}
}

func (b *browserBridge) Do(ctx context.Context, lease *infraegress.Lease, request *http.Request, timeout time.Duration) (*http.Response, error) {
	if b == nil {
		return nil, errors.New("浏览器桥接未配置")
	}
	body, err := readBridgeBody(request.Body)
	if err != nil {
		return nil, err
	}
	cookie := request.Header.Get("Cookie")
	payload := browserBridgeRequest{
		SessionKey: browserSessionKey(lease.ProxyURL, cookie), URL: request.URL.String(), Method: request.Method,
		Headers: browserFetchHeaders(request.Header), Body: base64.StdEncoding.EncodeToString(body),
		Cookie: cookie, ProxyURL: lease.ProxyURL, UserAgent: lease.UserAgent, Referer: request.Header.Get("Referer"),
		TimeoutMS: max(1000, timeout.Milliseconds()),
	}
	var result browserBridgeResponse
	if err := b.call(ctx, "/v1/fetch", payload, &result); err != nil {
		return nil, err
	}
	if result.Error != "" {
		return nil, fmt.Errorf("浏览器桥接请求失败: %s", result.Error)
	}
	if result.Status < 100 || result.Status > 599 {
		return nil, fmt.Errorf("浏览器桥接返回无效状态码 %d", result.Status)
	}
	decoded, err := base64.StdEncoding.DecodeString(result.Body)
	if err != nil {
		return nil, fmt.Errorf("解析浏览器桥接响应: %w", err)
	}
	return &http.Response{
		StatusCode: result.Status, Status: fmt.Sprintf("%d %s", result.Status, http.StatusText(result.Status)),
		Header: result.Headers.Clone(), Body: io.NopCloser(bytes.NewReader(decoded)), Request: request,
	}, nil
}

func (b *browserBridge) WebSocket(ctx context.Context, lease *infraegress.Lease, payload browserBridgeWebSocketRequest) ([][]byte, error) {
	if b == nil {
		return nil, errors.New("浏览器桥接未配置")
	}
	payload.ProxyURL = lease.ProxyURL
	payload.UserAgent = lease.UserAgent
	if payload.SessionKey == "" {
		payload.SessionKey = browserSessionKey(lease.ProxyURL, payload.Cookie)
	}
	var result browserBridgeWebSocketResponse
	if err := b.call(ctx, "/v1/websocket", payload, &result); err != nil {
		return nil, err
	}
	if result.Error != "" {
		return nil, fmt.Errorf("浏览器桥接 WebSocket 失败: %s", result.Error)
	}
	frames := make([][]byte, 0, len(result.Frames))
	for _, frame := range result.Frames {
		decoded, err := base64.StdEncoding.DecodeString(frame)
		if err != nil {
			return nil, fmt.Errorf("解析浏览器桥接 WebSocket 帧: %w", err)
		}
		frames = append(frames, decoded)
	}
	return frames, nil
}

func (b *browserBridge) call(ctx context.Context, path string, payload, output any) error {
	data, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	endpoint, err := url.JoinPath(b.baseURL, path)
	if err != nil {
		return err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(data))
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	if b.key != "" {
		request.Header.Set("Authorization", "Bearer "+b.key)
	}
	response, err := b.client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, browserBridgeBodyLimit+1))
	if err != nil {
		return err
	}
	if len(body) > browserBridgeBodyLimit {
		return errors.New("浏览器桥接响应超过 192 MiB")
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		var failure struct {
			Error string `json:"error"`
		}
		if json.Unmarshal(body, &failure) == nil && strings.TrimSpace(failure.Error) != "" {
			return fmt.Errorf("浏览器桥接返回 %d: %s", response.StatusCode, strings.TrimSpace(failure.Error))
		}
		return fmt.Errorf("浏览器桥接返回 %d", response.StatusCode)
	}
	if err := json.Unmarshal(body, output); err != nil {
		return fmt.Errorf("解析浏览器桥接响应: %w", err)
	}
	return nil
}

func readBridgeBody(body io.ReadCloser) ([]byte, error) {
	if body == nil {
		return nil, nil
	}
	defer body.Close()
	value, err := io.ReadAll(io.LimitReader(body, browserBridgeBodyLimit+1))
	if err != nil {
		return nil, err
	}
	if len(value) > browserBridgeBodyLimit {
		return nil, errors.New("浏览器桥接请求超过 192 MiB")
	}
	return value, nil
}

func browserFetchHeaders(source http.Header) http.Header {
	result := make(http.Header)
	for name, values := range source {
		lower := strings.ToLower(strings.TrimSpace(name))
		switch {
		case lower == "cookie", lower == "user-agent", lower == "referer", lower == "origin",
			lower == "host", lower == "content-length", lower == "accept-encoding", lower == "connection",
			lower == "priority", strings.HasPrefix(lower, "sec-fetch-"), strings.HasPrefix(lower, "sec-ch-ua"):
			continue
		}
		result[name] = append([]string(nil), values...)
	}
	return result
}

func browserSessionKey(proxyURL, cookie string) string {
	digest := sha256.Sum256([]byte(proxyURL + "\x00" + cookie))
	return hex.EncodeToString(digest[:16])
}
