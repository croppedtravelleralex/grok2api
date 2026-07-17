package cli

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
)

// normalizeResponsesRequest 改写路由字段和兼容别名，并为上游不支持的新工具协议建立请求级映射。
func normalizeResponsesRequest(body []byte, model string) ([]byte, *responsesToolCompatibility, error) {
	var payload map[string]json.RawMessage
	if err := json.Unmarshal(body, &payload); err != nil {
		return nil, nil, fmt.Errorf("解析 Responses 请求: %w", err)
	}
	payload["model"] = mustJSON(model)
	if responseFormat, exists := payload["response_format"]; exists {
		var text map[string]json.RawMessage
		if raw := payload["text"]; len(raw) > 0 && !bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
			if err := json.Unmarshal(raw, &text); err != nil {
				return nil, nil, fmt.Errorf("解析 text: %w", err)
			}
		}
		if text == nil {
			text = make(map[string]json.RawMessage)
		}
		if isEmptyJSON(text["format"]) {
			formatted, err := normalizeResponseFormat(responseFormat)
			if err != nil {
				return nil, nil, err
			}
			text["format"] = formatted
		}
		encoded, err := json.Marshal(text)
		if err != nil {
			return nil, nil, err
		}
		payload["text"] = encoded
		delete(payload, "response_format")
	}
	compatibility, err := normalizeResponsesTools(payload)
	if err != nil {
		return nil, nil, err
	}
	normalized, err := json.Marshal(payload)
	if err != nil {
		return nil, nil, err
	}
	return normalized, compatibility, nil
}

func normalizeResponseFormat(raw json.RawMessage) (json.RawMessage, error) {
	var format map[string]json.RawMessage
	if err := json.Unmarshal(raw, &format); err != nil {
		return nil, fmt.Errorf("解析 response_format: %w", err)
	}
	var formatType string
	_ = json.Unmarshal(format["type"], &formatType)
	if formatType != "json_schema" || isEmptyJSON(format["json_schema"]) {
		return raw, nil
	}
	var schema map[string]json.RawMessage
	if err := json.Unmarshal(format["json_schema"], &schema); err != nil {
		return nil, fmt.Errorf("解析 response_format.json_schema: %w", err)
	}
	result := make(map[string]json.RawMessage, len(schema)+1)
	result["type"] = mustJSON("json_schema")
	for key, value := range schema {
		result[key] = value
	}
	return json.Marshal(result)
}

func isEmptyJSON(raw json.RawMessage) bool {
	value := bytes.TrimSpace(raw)
	return len(value) == 0 || bytes.Equal(value, []byte("null")) || bytes.Equal(value, []byte(`""`))
}

// ensurePromptCacheKey 把 Anthropic cache_control 派生出的粘滞键写入 Responses 请求体，
// 便于上游返回 cached_tokens；若调用方已显式提供 prompt_cache_key 则不覆盖。
func ensurePromptCacheKey(body []byte, key string) ([]byte, error) {
	key = strings.TrimSpace(key)
	if key == "" {
		return body, nil
	}
	var payload map[string]json.RawMessage
	if err := json.Unmarshal(body, &payload); err != nil {
		return nil, fmt.Errorf("解析 Responses 请求以写入 prompt_cache_key: %w", err)
	}
	if raw, ok := payload["prompt_cache_key"]; ok && !isEmptyJSON(raw) {
		return body, nil
	}
	payload["prompt_cache_key"] = mustJSON(key)
	return json.Marshal(payload)
}

func mustJSON(value any) json.RawMessage {
	encoded, _ := json.Marshal(value)
	return encoded
}
