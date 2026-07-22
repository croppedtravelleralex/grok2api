package account

import (
	"bytes"
	"context"
	"encoding/base64"
	"fmt"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	accountapp "github.com/chenyme/grok2api/backend/internal/application/account"
	accountsyncapp "github.com/chenyme/grok2api/backend/internal/application/accountsync"
	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/infra/persistence/relational"
	"github.com/chenyme/grok2api/backend/internal/infra/provider"
	cliprovider "github.com/chenyme/grok2api/backend/internal/infra/provider/cli"
	webprovider "github.com/chenyme/grok2api/backend/internal/infra/provider/web"
	"github.com/chenyme/grok2api/backend/internal/infra/security"
	"github.com/gin-gonic/gin"
)

type accountSynchronizerStub struct {
	accountIDs []uint64
}

type accountProgressSynchronizerStub struct {
	accountSynchronizerStub
}

func TestNewAccountResponseIncludesImagineModelState(t *testing.T) {
	now := time.Now().UTC()
	response := newAccountResponse(accountapp.View{
		Credential: accountdomain.Credential{ID: 12, Provider: accountdomain.ProviderWeb},
		ModelStates: []accountdomain.ModelState{{
			AccountID: 12, UpstreamModel: "grok-imagine-image", Status: accountdomain.ModelStatusQuotaAvailable,
			Reason: "quota_remaining_positive", LastAttemptAt: now, LastSuccessAt: &now, UpdatedAt: now,
		}},
	})
	if len(response.ModelStates) != 1 {
		t.Fatalf("model states = %#v", response.ModelStates)
	}
	state := response.ModelStates[0]
	if state.UpstreamModel != "grok-imagine-image" || state.Status != "quota_available" || state.Reason != "quota_remaining_positive" || state.LastSuccessAt == nil {
		t.Fatalf("model state = %#v", state)
	}
	if response.Pool != "" {
		t.Fatalf("web account pool = %q, want empty", response.Pool)
	}
}

func TestAccountPoolOnlyAppliesToBuildProvider(t *testing.T) {
	webPool := accountPool(accountdomain.Credential{ID: 1, Provider: accountdomain.ProviderWeb, Enabled: true})
	if webPool != "" {
		t.Fatalf("web pool = %q", webPool)
	}
	buildPool := accountPool(accountdomain.Credential{
		ID: 2, Provider: accountdomain.ProviderBuild, Enabled: true, ObservedModel: "grok-4",
	})
	if buildPool != accountapp.PoolDispatch {
		t.Fatalf("build pool = %q", buildPool)
	}
}

func TestWriteServiceErrorUsesCredentialLimitCodes(t *testing.T) {
	gin.SetMode(gin.TestMode)
	tests := []struct {
		name string
		err  error
		code string
	}{
		{name: "import", err: fmt.Errorf("%w: too many", accountapp.ErrImportLimit), code: "accountImportLimitExceeded"},
		{name: "export", err: fmt.Errorf("%w: too many", accountapp.ErrExportLimit), code: "accountExportLimitExceeded"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			recorder := httptest.NewRecorder()
			ctx, _ := gin.CreateTestContext(recorder)
			new(Handler).writeServiceError(ctx, "fallback", test.err, 500, "failed")
			if recorder.Code != 400 || !strings.Contains(recorder.Body.String(), `"code":"`+test.code+`"`) {
				t.Fatalf("status = %d, body = %s", recorder.Code, recorder.Body.String())
			}
		})
	}
}

func (s *accountSynchronizerStub) Sync(_ context.Context, accountIDs ...uint64) accountsyncapp.Result {
	s.accountIDs = append(s.accountIDs, accountIDs...)
	return accountsyncapp.Result{Succeeded: len(accountIDs)}
}

func (s *accountSynchronizerStub) SyncStream(_ context.Context, accountIDs <-chan uint64) accountsyncapp.Result {
	for accountID := range accountIDs {
		s.accountIDs = append(s.accountIDs, accountID)
	}
	return accountsyncapp.Result{Succeeded: len(s.accountIDs)}
}

func (s *accountProgressSynchronizerStub) SyncStreamObserved(_ context.Context, accountIDs <-chan uint64, observer func(completed, total int)) accountsyncapp.Result {
	for accountID := range accountIDs {
		s.accountIDs = append(s.accountIDs, accountID)
	}
	for completed := 1; completed <= len(s.accountIDs); completed++ {
		observer(completed, completed)
	}
	return accountsyncapp.Result{Succeeded: len(s.accountIDs)}
}

func TestSyncInitialUsesOnlyChangedAccounts(t *testing.T) {
	sync := &accountSynchronizerStub{}
	handler := NewHandler(nil, sync)

	result := handler.syncInitial(context.Background(), 3, 5)

	if result.Succeeded != 2 || len(sync.accountIDs) != 2 || sync.accountIDs[0] != 3 || sync.accountIDs[1] != 5 {
		t.Fatalf("account ids = %#v", sync.accountIDs)
	}
}

func TestWriteBuildConversionEventUsesSSEFormat(t *testing.T) {
	gin.SetMode(gin.TestMode)
	recorder := httptest.NewRecorder()
	ctx, _ := gin.CreateTestContext(recorder)
	ctx.Request = httptest.NewRequest("POST", "/api/admin/v1/accounts/web/convert-to-build", nil)

	if err := writeAccountEvent(ctx, "progress", accountTaskProgressResponse{Completed: 3, Total: 10}); err != nil {
		t.Fatal(err)
	}
	if body := recorder.Body.String(); body != "event: progress\ndata: {\"completed\":3,\"total\":10}\n\n" {
		t.Fatalf("body = %q", body)
	}
}

func TestAccountProgressEventIncludesOptionalPhase(t *testing.T) {
	gin.SetMode(gin.TestMode)
	recorder := httptest.NewRecorder()
	ctx, _ := gin.CreateTestContext(recorder)
	ctx.Request = httptest.NewRequest("POST", "/api/admin/v1/accounts/import", nil)
	stream := &accountEventStream{context: ctx}
	var total atomic.Int64

	if err := stream.PhaseProgressObserver("importing", &total)(3, 10); err != nil {
		t.Fatal(err)
	}
	if body := recorder.Body.String(); body != "event: progress\ndata: {\"completed\":3,\"total\":10,\"phase\":\"importing\"}\n\n" {
		t.Fatalf("body = %q", body)
	}
	if total.Load() != 10 {
		t.Fatalf("total = %d", total.Load())
	}
}

func TestReadAccountImportDocumentsAcceptsMultipleFiles(t *testing.T) {
	gin.SetMode(gin.TestMode)
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	for name, value := range map[string]string{"first.json": `{"accounts":[]}`, "second.json": `{"provider":"grok_build"}`} {
		part, err := writer.CreateFormFile("files", name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := part.Write([]byte(value)); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	recorder := httptest.NewRecorder()
	ctx, _ := gin.CreateTestContext(recorder)
	ctx.Request = httptest.NewRequest("POST", "/api/admin/v1/accounts/import", &body)
	ctx.Request.Header.Set("Content-Type", writer.FormDataContentType())

	documents, ok := readAccountImportDocuments(ctx, "账号凭据 JSON")
	if !ok || len(documents) != 2 {
		t.Fatalf("documents = %q, status = %d", documents, recorder.Code)
	}
}

func TestReadAccountImportJSONAcceptsBuildPayload(t *testing.T) {
	gin.SetMode(gin.TestMode)
	payload := `{"provider":"grok_build","accounts":[{"refresh_token":"refresh-token"}]}`
	recorder := httptest.NewRecorder()
	ctx, _ := gin.CreateTestContext(recorder)
	ctx.Request = httptest.NewRequest(http.MethodPost, "/api/admin/v1/accounts/import-json", strings.NewReader(payload))

	data, provider, ok := readAccountImportJSON(ctx)
	if !ok {
		t.Fatalf("status = %d, body = %s", recorder.Code, recorder.Body.String())
	}
	if string(data) != payload || provider != accountdomain.ProviderBuild {
		t.Fatalf("provider = %q, data = %s", provider, data)
	}
}

func TestReadAccountImportJSONRejectsMissingProvider(t *testing.T) {
	gin.SetMode(gin.TestMode)
	recorder := httptest.NewRecorder()
	ctx, _ := gin.CreateTestContext(recorder)
	ctx.Request = httptest.NewRequest(http.MethodPost, "/api/admin/v1/accounts/import-json", strings.NewReader(`{"accounts":[]}`))

	if _, _, ok := readAccountImportJSON(ctx); ok {
		t.Fatal("expected missing provider to be rejected")
	}
	if recorder.Code != http.StatusBadRequest || !strings.Contains(recorder.Body.String(), `"code":"invalidAccountProvider"`) {
		t.Fatalf("status = %d, body = %s", recorder.Code, recorder.Body.String())
	}
}

func TestImportJSONRouteRejectsUnsupportedProvider(t *testing.T) {
	gin.SetMode(gin.TestMode)
	router := gin.New()
	NewHandler(nil, nil).Register(router.Group("/api/admin/v1"))
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/admin/v1/accounts/import-json", strings.NewReader(`{"provider":"other","accounts":[]}`))
	request.Header.Set("Content-Type", "application/json")

	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusBadRequest || !strings.Contains(recorder.Body.String(), `"code":"invalidAccountProvider"`) {
		t.Fatalf("status = %d, body = %s", recorder.Code, recorder.Body.String())
	}
}

func TestImportJSONRoutePersistsAndQueuesBuildAccount(t *testing.T) {
	gin.SetMode(gin.TestMode)
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "account-import.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	cipher, err := security.NewCipher(base64.StdEncoding.EncodeToString(make([]byte, 32)))
	if err != nil {
		t.Fatal(err)
	}
	repository := relational.NewAccountRepository(database)
	adapter := cliprovider.NewAdapter(cliprovider.Config{}, cipher)
	service := accountapp.NewService(repository, nil, nil, nil, provider.NewRegistry(adapter), cipher, nil)
	syncer := &accountSynchronizerStub{}
	router := gin.New()
	NewHandler(service, syncer).Register(router.Group("/api/admin/v1"))
	recorder := httptest.NewRecorder()
	payload := `{"provider":"grok_build","accounts":[{"name":"api-import","refresh_token":"refresh-token","user_id":"user-1"}]}`
	request := httptest.NewRequest(http.MethodPost, "/api/admin/v1/accounts/import-json", strings.NewReader(payload))
	request.Header.Set("Content-Type", "application/json")

	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK || !strings.Contains(recorder.Body.String(), `"provider":"grok_build"`) || !strings.Contains(recorder.Body.String(), `"created":1`) || !strings.Contains(recorder.Body.String(), `"synced":1`) {
		t.Fatalf("status = %d, body = %s", recorder.Code, recorder.Body.String())
	}
	if len(syncer.accountIDs) != 1 {
		t.Fatalf("queued account IDs = %#v", syncer.accountIDs)
	}
}

func TestReauthenticateRouteReplacesWebSSOWithoutCreatingDuplicate(t *testing.T) {
	gin.SetMode(gin.TestMode)
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "account-reauth.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	cipher, err := security.NewCipher(base64.StdEncoding.EncodeToString(make([]byte, 32)))
	if err != nil {
		t.Fatal(err)
	}
	repository := relational.NewAccountRepository(database)
	service := accountapp.NewService(repository, relational.NewAuditRepository(database), nil, nil, provider.NewRegistry(webprovider.NewAdapter(webprovider.Config{}, nil, cipher, nil, nil)), cipher, nil)
	imported, err := service.ImportWebCredentials(ctx, []byte("old-sso-token"))
	if err != nil {
		t.Fatal(err)
	}
	accountID := imported.AccountIDs[0]
	if err := service.MarkReauthRequired(ctx, accountID, "expired"); err != nil {
		t.Fatal(err)
	}
	syncer := &accountSynchronizerStub{}
	router := gin.New()
	NewHandler(service, syncer).Register(router.Group("/api/admin/v1"))
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, fmt.Sprintf("/api/admin/v1/accounts/%d/reauth", accountID), strings.NewReader(`{"ssoToken":"fresh-sso-token","webTier":"super"}`))
	request.Header.Set("Content-Type", "application/json")

	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK || !strings.Contains(recorder.Body.String(), `"authStatus":"active"`) || !strings.Contains(recorder.Body.String(), `"syncFailed":0`) {
		t.Fatalf("status = %d, body = %s", recorder.Code, recorder.Body.String())
	}
	values, total, err := service.List(ctx, 1, 20, "", accountapp.ListFilter{Provider: string(accountdomain.ProviderWeb)})
	if err != nil || total != 1 || len(values) != 1 || values[0].Credential.ID != accountID {
		t.Fatalf("accounts = %#v, total = %d, err = %v", values, total, err)
	}
}

func TestAccountAnalyticsRouteReturnsCurrentSnapshot(t *testing.T) {
	gin.SetMode(gin.TestMode)
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "account-analytics.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	cipher, err := security.NewCipher(base64.StdEncoding.EncodeToString(make([]byte, 32)))
	if err != nil {
		t.Fatal(err)
	}
	repository := relational.NewAccountRepository(database)
	service := accountapp.NewService(repository, nil, nil, nil, provider.NewRegistry(cliprovider.NewAdapter(cliprovider.Config{}, cipher)), cipher, nil)
	if _, err := service.ImportCredentials(ctx, []byte(`{"name":"build","user_id":"user-analytics","refresh_token":"refresh"}`)); err != nil {
		t.Fatal(err)
	}
	router := gin.New()
	NewHandler(service, nil).Register(router.Group("/api/admin/v1"))
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/admin/v1/accounts/analytics?period=24h", nil)

	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK || !strings.Contains(recorder.Body.String(), `"intervalMinutes":15`) || !strings.Contains(recorder.Body.String(), `"provider":"grok_build"`) {
		t.Fatalf("status = %d, body = %s", recorder.Code, recorder.Body.String())
	}
}

func TestBuildProbeStatusRouteReturnsPoolStatisticsWithoutNullTimes(t *testing.T) {
	gin.SetMode(gin.TestMode)
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "build-probe-status.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	repository := relational.NewAccountRepository(database)
	service := accountapp.NewService(repository, nil, nil, nil, provider.NewRegistry(cliprovider.NewAdapter(cliprovider.Config{}, nil)), nil, nil)
	if _, _, err := repository.UpsertByIdentity(ctx, accountdomain.Credential{
		Provider: accountdomain.ProviderBuild, AuthType: accountdomain.AuthTypeOAuth, Name: "pending", SourceKey: "pending",
		EncryptedAccessToken: "token", Enabled: true, AuthStatus: accountdomain.AuthStatusActive,
	}); err != nil {
		t.Fatal(err)
	}
	router := gin.New()
	NewHandler(service, nil).Register(router.Group("/api/admin/v1"))
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/admin/v1/accounts/build-probe", nil)

	router.ServeHTTP(recorder, request)

	body := recorder.Body.String()
	if recorder.Code != http.StatusOK || !strings.Contains(body, `"enabled":false`) || !strings.Contains(body, `"verification":1`) || !strings.Contains(body, `"purgeApply":false`) {
		t.Fatalf("status = %d, body = %s", recorder.Code, body)
	}
	if strings.Contains(body, `"startedAt":null`) || strings.Contains(body, `"nextRunAt":null`) || strings.Contains(body, `"current":null`) {
		t.Fatalf("optional fields must be omitted: %s", body)
	}

	patch := httptest.NewRecorder()
	patchRequest := httptest.NewRequest(http.MethodPatch, "/api/admin/v1/accounts/build-probe", strings.NewReader(`{"purgeApply":true}`))
	patchRequest.Header.Set("Content-Type", "application/json")
	router.ServeHTTP(patch, patchRequest)
	if patch.Code != http.StatusOK || !strings.Contains(patch.Body.String(), `"purgeApply":true`) {
		t.Fatalf("patch status = %d, body = %s", patch.Code, patch.Body.String())
	}
}

func TestWebProbeStatusRouteReturnsSixPoolSummary(t *testing.T) {
	gin.SetMode(gin.TestMode)
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "web-probe-status.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	repository := relational.NewAccountRepository(database)
	service := accountapp.NewService(repository, nil, nil, nil, provider.NewRegistry(), nil, nil)
	if _, _, err := repository.UpsertByIdentity(ctx, accountdomain.Credential{
		Provider: accountdomain.ProviderWeb, AuthType: accountdomain.AuthTypeSSO, Name: "web-1", SourceKey: "web-1",
		EncryptedAccessToken: "token", Enabled: true, AuthStatus: accountdomain.AuthStatusActive,
	}); err != nil {
		t.Fatal(err)
	}
	router := gin.New()
	NewHandler(service, nil).Register(router.Group("/api/admin/v1"))
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/admin/v1/accounts/web-probe", nil)
	router.ServeHTTP(recorder, request)
	body := recorder.Body.String()
	if recorder.Code != http.StatusOK || !strings.Contains(body, `"image"`) || !strings.Contains(body, `"chat"`) || !strings.Contains(body, `"maxProbeLevel"`) {
		t.Fatalf("status = %d, body = %s", recorder.Code, body)
	}
}

func TestAccountSyncPipelineUsesFinalQueuedTotal(t *testing.T) {
	syncer := &accountProgressSynchronizerStub{}
	handler := NewHandler(nil, syncer)
	progress := make([][2]int, 0, 5)
	pipeline := handler.startSyncPipeline(context.Background(), func(completed, total int) {
		progress = append(progress, [2]int{completed, total})
	})

	for _, accountID := range []uint64{11, 12, 13} {
		if err := pipeline.Observe(accountID); err != nil {
			t.Fatal(err)
		}
	}
	result := pipeline.Finish(false)

	if result.Succeeded != 3 {
		t.Fatalf("result = %#v", result)
	}
	if len(progress) == 0 || progress[len(progress)-1] != [2]int{3, 3} {
		t.Fatalf("progress = %#v", progress)
	}
	for _, value := range progress {
		if value[1] != 3 {
			t.Fatalf("progress contains changing total: %#v", progress)
		}
	}
}
