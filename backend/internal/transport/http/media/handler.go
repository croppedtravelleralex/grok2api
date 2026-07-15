package media

import (
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	mediaapp "github.com/chenyme/grok2api/backend/internal/application/media"
	"github.com/chenyme/grok2api/backend/internal/shared/response"
	"github.com/gin-gonic/gin"
)

type Handler struct {
	service *mediaapp.Service
}

func NewHandler(service *mediaapp.Service) *Handler { return &Handler{service: service} }

// RegisterPublic 注册使用不可猜测资源 ID 的公开图片读取端点。
func (h *Handler) RegisterPublic(router *gin.Engine) {
	router.GET("/v1/media/images/:assetId", h.getImage)
	router.HEAD("/v1/media/images/:assetId", h.getImage)
}

func (h *Handler) RegisterAdmin(router *gin.RouterGroup) {
	router.GET("/media/images", h.listImages)
	router.GET("/media/images/stats", h.imageStats)
	router.DELETE("/media/images", h.deleteImages)
	router.DELETE("/media/images/:assetId", h.deleteImage)
}

type imageDTO struct {
	ID                   string `json:"id"`
	URL                  string `json:"url"`
	MIMEType             string `json:"mimeType"`
	SizeBytes            int64  `json:"sizeBytes"`
	RequestID            string `json:"requestId,omitempty"`
	Model                string `json:"model,omitempty"`
	Resolution           string `json:"resolution,omitempty"`
	Width                int    `json:"width"`
	Height               int    `json:"height"`
	GenerationDurationMS int64  `json:"generationDurationMs"`
	CreatedAt            string `json:"createdAt"`
}

func (h *Handler) listImages(c *gin.Context) {
	page, _ := strconv.Atoi(c.DefaultQuery("page", "1"))
	pageSize, _ := strconv.Atoi(c.DefaultQuery("pageSize", "20"))
	from, to, err := parseTimeRange(c)
	if err != nil {
		response.Error(c, http.StatusBadRequest, "invalidMediaDateRange", "图片日期范围无效")
		return
	}
	result, err := h.service.ListImagesInRange(c.Request.Context(), page, pageSize, from, to)
	if err != nil {
		response.Error(c, http.StatusInternalServerError, "mediaListFailed", "读取图片历史失败")
		return
	}
	items := make([]imageDTO, 0, len(result.Items))
	for _, item := range result.Items {
		items = append(items, imageDTO{ID: item.ID, URL: item.URL, MIMEType: item.MIMEType, SizeBytes: item.SizeBytes,
			RequestID: item.RequestID, Model: item.Model, Resolution: item.Resolution, Width: item.Width, Height: item.Height,
			GenerationDurationMS: item.GenerationDurationMS, CreatedAt: item.CreatedAt.Format(time.RFC3339Nano)})
	}
	response.Success(c, http.StatusOK, gin.H{"items": items, "page": result.Page, "pageSize": result.PageSize, "total": result.Total})
}

func (h *Handler) deleteImages(c *gin.Context) {
	from, to, err := parseTimeRange(c)
	if err != nil {
		response.Error(c, http.StatusBadRequest, "invalidMediaDateRange", "图片日期范围无效")
		return
	}
	result, err := h.service.DeleteImages(c.Request.Context(), from, to)
	if err != nil {
		response.Error(c, http.StatusInternalServerError, "mediaDeleteFailed", "批量删除图片失败")
		return
	}
	response.Success(c, http.StatusOK, gin.H{"deleted": result.Deleted, "totalBytes": result.TotalBytes})
}

func parseTimeRange(c *gin.Context) (*time.Time, *time.Time, error) {
	parse := func(value string) (*time.Time, error) {
		value = strings.TrimSpace(value)
		if value == "" {
			return nil, nil
		}
		parsed, err := time.Parse(time.RFC3339, value)
		if err != nil {
			return nil, err
		}
		parsed = parsed.UTC()
		return &parsed, nil
	}
	from, err := parse(c.Query("from"))
	if err != nil {
		return nil, nil, err
	}
	to, err := parse(c.Query("to"))
	if err != nil {
		return nil, nil, err
	}
	if from != nil && to != nil && !from.Before(*to) {
		return nil, nil, errors.New("开始时间必须早于结束时间")
	}
	return from, to, nil
}

func (h *Handler) imageStats(c *gin.Context) {
	result, err := h.service.ImageStats(c.Request.Context())
	if err != nil {
		response.Error(c, http.StatusInternalServerError, "mediaStatsFailed", "读取图片统计失败")
		return
	}
	response.Success(c, http.StatusOK, gin.H{"count": result.Count, "totalBytes": result.TotalBytes})
}

func (h *Handler) deleteImage(c *gin.Context) {
	err := h.service.DeleteImage(c.Request.Context(), c.Param("assetId"))
	if errors.Is(err, mediaapp.ErrAssetNotFound) {
		response.Error(c, http.StatusNotFound, "mediaNotFound", "图片不存在")
		return
	}
	if err != nil {
		response.Error(c, http.StatusInternalServerError, "mediaDeleteFailed", "删除图片失败")
		return
	}
	response.Success(c, http.StatusOK, gin.H{"deleted": true})
}

func (h *Handler) getImage(c *gin.Context) {
	asset, body, err := h.service.OpenImage(c.Request.Context(), c.Param("assetId"))
	if errors.Is(err, mediaapp.ErrAssetNotFound) {
		c.Status(http.StatusNotFound)
		return
	}
	if err != nil {
		c.Status(http.StatusInternalServerError)
		return
	}
	defer body.Close()
	etag := `"` + asset.SHA256 + `"`
	if strings.TrimSpace(c.GetHeader("If-None-Match")) == etag {
		c.Header("ETag", etag)
		c.Status(http.StatusNotModified)
		return
	}
	c.Header("Content-Type", asset.MIMEType)
	c.Header("Content-Length", strconv.FormatInt(asset.SizeBytes, 10))
	c.Header("Cache-Control", "public, max-age=31536000, immutable")
	c.Header("ETag", etag)
	c.Header("X-Content-Type-Options", "nosniff")
	if c.Request.Method == http.MethodHead {
		c.Status(http.StatusOK)
		return
	}
	c.Status(http.StatusOK)
	_, _ = io.Copy(c.Writer, body)
}
