package main

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"time"

	httpclient "github.com/bogdanfinn/fhttp"
	tls_client "github.com/bogdanfinn/tls-client"
	"github.com/bogdanfinn/tls-client/profiles"
)

func main() {
	proxy := "http://127.0.0.1:7897"
	if v := os.Getenv("HTTPS_PROXY"); v != "" {
		proxy = v
	}
	options := []tls_client.HttpClientOption{
		tls_client.WithTimeoutSeconds(25),
		tls_client.WithClientProfile(profiles.Chrome_146),
		tls_client.WithNotFollowRedirects(),
		tls_client.WithProxyUrl(proxy),
	}
	client, err := tls_client.NewHttpClient(tls_client.NewNoopLogger(), options...)
	if err != nil {
		panic(err)
	}
	req, err := httpclient.NewRequest(http.MethodGet, "https://grok.com/", nil)
	if err != nil {
		panic(err)
	}
	req.Header = httpclient.Header{
		"accept":          {"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
		"accept-language": {"en-US,en;q=0.9"},
		"user-agent":      {"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"},
	}
	started := time.Now()
	resp, err := client.Do(req)
	if err != nil {
		fmt.Println("tls_client_error", err)
		os.Exit(1)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 400))
	prefix := string(body)
	if len(prefix) > 180 {
		prefix = prefix[:180]
	}
	fmt.Printf("proxy=%s status=%d elapsed=%s cf-mitigated=%q server=%q body=%q\n",
		proxy, resp.StatusCode, time.Since(started).Round(time.Millisecond),
		resp.Header.Get("cf-mitigated"), resp.Header.Get("server"), prefix)

	req2, _ := httpclient.NewRequest(http.MethodPost, "https://grok.com/rest/app-chat/conversations/new", nil)
	req2.Header = httpclient.Header{
		"accept":       {"application/json"},
		"content-type": {"application/json"},
		"origin":       {"https://grok.com"},
		"referer":      {"https://grok.com/"},
		"user-agent":   {"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"},
	}
	resp2, err := client.Do(req2)
	if err != nil {
		fmt.Println("rest_error", err)
		os.Exit(1)
	}
	defer resp2.Body.Close()
	body2, _ := io.ReadAll(io.LimitReader(resp2.Body, 300))
	fmt.Printf("rest_status=%d cf-mitigated=%q body=%s\n", resp2.StatusCode, resp2.Header.Get("cf-mitigated"), string(body2))
}
