package media

import (
	"bytes"
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	mediadomain "github.com/chenyme/grok2api/backend/internal/domain/media"
	localmedia "github.com/chenyme/grok2api/backend/internal/infra/media"
	"github.com/chenyme/grok2api/backend/internal/infra/persistence/relational"
)

const onePixelPNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="

func TestServicePersistsAndReopensImage(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "media.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	objects, err := localmedia.NewLocalStore(filepath.Join(t.TempDir(), "objects"))
	if err != nil {
		t.Fatal(err)
	}
	service := NewService(relational.NewMediaAssetRepository(database), objects, nil, Config{
		PublicBaseURL: "https://api.example", MaxImageBytes: 32 << 20, MaxTotalBytes: 1 << 30,
		CleanupThresholdPercent: 80, CleanupInterval: 10 * time.Minute,
	})
	raw, _ := base64.StdEncoding.DecodeString(onePixelPNG)
	asset, err := service.SaveImage(ctx, raw)
	if err != nil {
		t.Fatal(err)
	}
	if asset.MIMEType != "image/png" || asset.SizeBytes != int64(len(raw)) || len(asset.SHA256) != 64 {
		t.Fatalf("asset = %#v", asset)
	}
	if got := service.PublicImageURL(asset.ID); got != "https://api.example/v1/media/images/"+asset.ID {
		t.Fatalf("public URL = %q", got)
	}
	stored, body, err := service.OpenImage(ctx, asset.ID)
	if err != nil {
		t.Fatal(err)
	}
	data, err := io.ReadAll(body)
	_ = body.Close()
	if err != nil || stored.ID != asset.ID || !bytes.Equal(data, raw) {
		t.Fatalf("stored=%#v size=%d err=%v", stored, len(data), err)
	}
	if _, err := service.SaveImage(ctx, []byte("not an image")); err == nil {
		t.Fatal("invalid image content was accepted")
	}
}

func TestServicePersistsImageMetadataAndDimensions(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "media-metadata.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	objects, err := localmedia.NewLocalStore(filepath.Join(t.TempDir(), "objects"))
	if err != nil {
		t.Fatal(err)
	}
	service := NewService(relational.NewMediaAssetRepository(database), objects, nil, Config{PublicBaseURL: "https://api.example", MaxImageBytes: 32 << 20, MaxTotalBytes: 1 << 30, CleanupThresholdPercent: 80, CleanupInterval: 10 * time.Minute})
	raw, _ := base64.StdEncoding.DecodeString(onePixelPNG)
	asset, err := service.SaveImageWithMetadata(ctx, raw, mediadomain.AssetMetadata{RequestID: "req-image", Model: "grok-imagine-image", Resolution: "1k", StartedAt: time.Now().Add(-1500 * time.Millisecond)})
	if err != nil {
		t.Fatal(err)
	}
	if asset.RequestID != "req-image" || asset.Model != "grok-imagine-image" || asset.Resolution != "1k" || asset.Width != 1 || asset.Height != 1 || asset.GenerationDurationMS < 1000 {
		t.Fatalf("asset metadata = %#v", asset)
	}
	page, err := service.ListImages(ctx, 1, 20)
	if err != nil || len(page.Items) != 1 || page.Items[0].Width != 1 || page.Items[0].GenerationDurationMS == 0 {
		t.Fatalf("page=%#v err=%v", page, err)
	}
}

func TestServiceDeletesImagesByDateRange(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "media-range.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	objects, err := localmedia.NewLocalStore(filepath.Join(t.TempDir(), "objects"))
	if err != nil {
		t.Fatal(err)
	}
	repository := relational.NewMediaAssetRepository(database)
	raw, _ := base64.StdEncoding.DecodeString(onePixelPNG)
	day := time.Date(2026, 7, 14, 0, 0, 0, 0, time.UTC)
	for index, createdAt := range []time.Time{day.Add(-time.Hour), day.Add(time.Hour), day.Add(25 * time.Hour)} {
		id := fmt.Sprintf("img_range_%016d", index)
		key, saveErr := objects.SaveImage(ctx, id, "image/png", raw)
		if saveErr != nil {
			t.Fatal(saveErr)
		}
		if createErr := repository.CreateMediaAsset(ctx, mediadomain.Asset{ID: id, Kind: "image", StorageKey: key, MIMEType: "image/png", SizeBytes: int64(len(raw)), SHA256: strings.Repeat("a", 64), CreatedAt: createdAt}); createErr != nil {
			t.Fatal(createErr)
		}
	}
	service := NewService(repository, objects, nil, Config{PublicBaseURL: "https://api.example", MaxImageBytes: 32 << 20, MaxTotalBytes: 1 << 30, CleanupThresholdPercent: 80, CleanupInterval: 10 * time.Minute})
	to := day.Add(24 * time.Hour)
	deleted, err := service.DeleteImages(ctx, &day, &to)
	if err != nil || deleted.Deleted != 1 {
		t.Fatalf("deleted=%#v err=%v", deleted, err)
	}
	page, err := service.ListImages(ctx, 1, 20)
	if err != nil || page.Total != 2 {
		t.Fatalf("page=%#v err=%v", page, err)
	}
}

func TestServiceListsStatsAndDeletesImages(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "media-admin.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	objects, err := localmedia.NewLocalStore(filepath.Join(t.TempDir(), "objects"))
	if err != nil {
		t.Fatal(err)
	}
	service := NewService(relational.NewMediaAssetRepository(database), objects, nil, Config{
		PublicBaseURL: "https://api.example", MaxImageBytes: 32 << 20, MaxTotalBytes: 1 << 30,
		CleanupThresholdPercent: 80, CleanupInterval: 10 * time.Minute,
	})
	raw, _ := base64.StdEncoding.DecodeString(onePixelPNG)
	first, err := service.SaveImage(ctx, raw)
	if err != nil {
		t.Fatal(err)
	}
	second, err := service.SaveImage(ctx, raw)
	if err != nil {
		t.Fatal(err)
	}

	page, err := service.ListImages(ctx, 1, 1)
	if err != nil {
		t.Fatal(err)
	}
	if page.Total != 2 || len(page.Items) != 1 || page.Items[0].ID != second.ID || page.Items[0].URL != service.PublicImageURL(second.ID) {
		t.Fatalf("page = %#v", page)
	}
	stats, err := service.ImageStats(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if stats.Count != 2 || stats.TotalBytes != int64(len(raw)*2) {
		t.Fatalf("stats = %#v", stats)
	}
	if err := service.DeleteImage(ctx, first.ID); err != nil {
		t.Fatal(err)
	}
	if _, _, err := service.OpenImage(ctx, first.ID); !errors.Is(err, ErrAssetNotFound) {
		t.Fatalf("deleted image still opens: %v", err)
	}
	if err := service.DeleteImage(ctx, first.ID); !errors.Is(err, ErrAssetNotFound) {
		t.Fatalf("second delete error = %v", err)
	}
}

func TestCleanupDeletesOldestAssetsAtThreshold(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "media-cleanup.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	objects, err := localmedia.NewLocalStore(filepath.Join(t.TempDir(), "objects"))
	if err != nil {
		t.Fatal(err)
	}
	repository := relational.NewMediaAssetRepository(database)
	raw, _ := base64.StdEncoding.DecodeString(onePixelPNG)
	now := time.Now().UTC()
	ids := []string{"img_cleanup_0000000000000001", "img_cleanup_0000000000000002", "img_cleanup_0000000000000003", "img_cleanup_0000000000000004"}
	for index, id := range ids {
		key, err := objects.SaveImage(ctx, id, "image/png", raw)
		if err != nil {
			t.Fatal(err)
		}
		createdAt := now.Add(time.Duration(index-4) * time.Hour)
		if index == len(ids)-1 {
			createdAt = now
		}
		if err := repository.CreateMediaAsset(ctx, mediadomain.Asset{
			ID: id, Kind: "image", StorageKey: key, MIMEType: "image/png", SizeBytes: int64(len(raw)),
			SHA256: strings.Repeat("a", 64), CreatedAt: createdAt,
		}); err != nil {
			t.Fatal(err)
		}
	}
	service := NewService(repository, objects, nil, Config{
		PublicBaseURL: "https://api.example", MaxImageBytes: 32 << 20,
		MaxTotalBytes: int64(len(raw) * 2), CleanupThresholdPercent: 50,
		CleanupInterval: 10 * time.Minute,
	})
	deleted, err := service.Cleanup(ctx)
	if err != nil || deleted != 3 {
		t.Fatalf("deleted=%d err=%v", deleted, err)
	}
	total, err := repository.TotalMediaAssetBytes(ctx)
	if err != nil || total != int64(len(raw)) {
		t.Fatalf("remaining bytes=%d err=%v", total, err)
	}
	if _, _, err := service.OpenImage(ctx, ids[0]); !errors.Is(err, ErrAssetNotFound) {
		t.Fatalf("oldest asset still exists: %v", err)
	}
	if _, body, err := service.OpenImage(ctx, ids[3]); err != nil {
		t.Fatalf("recent asset was deleted: %v", err)
	} else {
		_ = body.Close()
	}
}

func TestCleanupPreservesMetadataWhenLocalObjectIsMissing(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "media-missing.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	objects, err := localmedia.NewLocalStore(filepath.Join(t.TempDir(), "objects"))
	if err != nil {
		t.Fatal(err)
	}
	repository := relational.NewMediaAssetRepository(database)
	raw, _ := base64.StdEncoding.DecodeString(onePixelPNG)
	id := "img_missing_0000000000000001"
	key, err := objects.SaveImage(ctx, id, "image/png", raw)
	if err != nil {
		t.Fatal(err)
	}
	if err := repository.CreateMediaAsset(ctx, mediadomain.Asset{ID: id, Kind: "image", StorageKey: key, MIMEType: "image/png", SizeBytes: int64(len(raw)), SHA256: strings.Repeat("a", 64), CreatedAt: time.Now().UTC()}); err != nil {
		t.Fatal(err)
	}
	if err := objects.Delete(ctx, key); err != nil {
		t.Fatal(err)
	}
	service := NewService(repository, objects, nil, Config{PublicBaseURL: "https://api.example", MaxImageBytes: 32 << 20, MaxTotalBytes: int64(len(raw)), CleanupThresholdPercent: 50, CleanupInterval: 10 * time.Minute})
	if _, err := service.Cleanup(ctx); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("cleanup error = %v", err)
	}
	if _, err := repository.GetMediaAsset(ctx, id); err != nil {
		t.Fatalf("shared metadata was deleted: %v", err)
	}
}
