package web

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	infraegress "github.com/chenyme/grok2api/backend/internal/infra/egress"
)

func TestBrowserBridgeDoUsesAuthenticatedBrowserPayload(t *testing.T) {
	var captured browserBridgeRequest
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer bridge-secret" {
			t.Fatalf("authorization = %q", request.Header.Get("Authorization"))
		}
		if err := json.NewDecoder(request.Body).Decode(&captured); err != nil {
			t.Fatal(err)
		}
		_ = json.NewEncoder(writer).Encode(browserBridgeResponse{
			Status:  http.StatusCreated,
			Headers: http.Header{"Content-Type": []string{"application/json"}},
			Body:    base64.StdEncoding.EncodeToString([]byte(`{"ok":true}`)),
		})
	}))
	defer server.Close()

	bridge := newBrowserBridge(server.URL, "bridge-secret")
	request, err := http.NewRequestWithContext(context.Background(), http.MethodPost, "https://grok.com/rest/app-chat/conversations/new", strings.NewReader(`{"message":"hello"}`))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Cookie", "sso=secret-token; cf_clearance=clearance")
	request.Header.Set("User-Agent", "Chrome/146")
	request.Header.Set("Referer", "https://grok.com/")
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("x-statsig-id", "signed")
	lease := &infraegress.Lease{NodeID: 7, ProxyURL: "http://user:pass@proxy.example:8080", UserAgent: "Chrome/146"}

	response, err := bridge.Do(context.Background(), lease, request, 30*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, _ := io.ReadAll(response.Body)
	if response.StatusCode != http.StatusCreated || string(body) != `{"ok":true}` {
		t.Fatalf("response = %d %s", response.StatusCode, body)
	}
	if captured.URL != request.URL.String() || captured.Method != http.MethodPost || captured.ProxyURL != lease.ProxyURL {
		t.Fatalf("captured request = %#v", captured)
	}
	if captured.Cookie != request.Header.Get("Cookie") || captured.UserAgent != lease.UserAgent || captured.Referer != request.Header.Get("Referer") {
		t.Fatalf("browser identity was not forwarded: %#v", captured)
	}
	if captured.Headers.Get("Cookie") != "" || captured.Headers.Get("User-Agent") != "" || captured.Headers.Get("Referer") != "" {
		t.Fatalf("browser-owned headers leaked into fetch headers: %#v", captured.Headers)
	}
	if captured.Headers.Get("Content-Type") != "application/json" || captured.Headers.Get("x-statsig-id") != "signed" {
		t.Fatalf("application headers = %#v", captured.Headers)
	}
	decoded, _ := base64.StdEncoding.DecodeString(captured.Body)
	if string(decoded) != `{"message":"hello"}` || captured.TimeoutMS != 30000 || captured.SessionKey == "" {
		t.Fatalf("captured body/session = %#v body=%s", captured, decoded)
	}
}

func TestBrowserBridgeWebSocketReturnsTextFrames(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		_ = json.NewEncoder(writer).Encode(browserBridgeWebSocketResponse{Frames: []string{
			base64.StdEncoding.EncodeToString([]byte(`{"type":"image"}`)),
			base64.StdEncoding.EncodeToString([]byte(`{"type":"json"}`)),
		}})
	}))
	defer server.Close()

	bridge := newBrowserBridge(server.URL, "")
	frames, err := bridge.WebSocket(context.Background(), &infraegress.Lease{ProxyURL: "direct://"}, browserBridgeWebSocketRequest{
		URL: "wss://grok.com/ws/imagine/listen", Cookie: "sso=value", TimeoutMS: 1000,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(frames) != 2 || string(frames[0]) != `{"type":"image"}` || string(frames[1]) != `{"type":"json"}` {
		t.Fatalf("frames = %#v", frames)
	}
}
