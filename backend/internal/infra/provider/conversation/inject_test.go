package conversation

import (
	"encoding/json"
	"testing"
)

func TestSetBootstrapSystemPromptDisabledByDefault(t *testing.T) {
	SetBootstrapSystemPrompt("")
	if bootstrapSystemPrompt() != "" {
		t.Fatalf("默认应为空（不注入）")
	}
}

func TestConvertChatRequestInsertsSystemPrompt(t *testing.T) {
	SetBootstrapSystemPrompt("务必如实报告工具失败，禁止编造数值。")
	defer SetBootstrapSystemPrompt("")

	body := []byte(`{"model":"grok-4.6","messages":[{"role":"user","content":"2+2"}]}`)
	converted, err := ConvertRequest(body, "grok-4.6", OperationChat)
	if err != nil {
		t.Fatalf("转换失败: %v", err)
	}
	var out map[string]json.RawMessage
	if err := json.Unmarshal(converted, &out); err != nil {
		t.Fatalf("输出非法 JSON: %v", err)
	}
	var input []map[string]any
	if err := json.Unmarshal(out["input"], &input); err != nil {
		t.Fatalf("input 非法: %v", err)
	}
	if len(input) != 2 {
		t.Fatalf("应注入 1 条 system + 原 1 条 user，共 2 条，实际 %d", len(input))
	}
	first := input[0]
	if first["type"] != "message" || first["role"] != "system" {
		t.Fatalf("首条应为 system 消息，实际 %#v", first)
	}
	if got := first["content"]; got != "务必如实报告工具失败，禁止编造数值。" {
		t.Fatalf("注入文本不符：%#v", got)
	}
	if input[1]["role"] != "user" {
		t.Fatalf("原 user 消息位置被破坏: %#v", input[1])
	}
}

func TestConvertChatRequestNoInjectionWhenDisabled(t *testing.T) {
	SetBootstrapSystemPrompt("")
	body := []byte(`{"model":"grok-4.6","messages":[{"role":"user","content":"hi"}]}`)
	converted, err := ConvertRequest(body, "grok-4.6", OperationChat)
	if err != nil {
		t.Fatalf("转换失败: %v", err)
	}
	var out map[string]json.RawMessage
	if err := json.Unmarshal(converted, &out); err != nil {
		t.Fatalf("输出非法 JSON: %v", err)
	}
	var input []map[string]any
	if err := json.Unmarshal(out["input"], &input); err != nil {
		t.Fatalf("input 非法: %v", err)
	}
	if len(input) != 1 || input[0]["role"] != "user" {
		t.Fatalf("未开启注入时应保持原样 1 条 user，实际 %d 条, %#v", len(input), input)
	}
}

func TestInjectDoesNotAffectResponsesOperation(t *testing.T) {
	SetBootstrapSystemPrompt("严禁编造。")
	defer SetBootstrapSystemPrompt("")
	body := []byte(`{"model":"grok-4.6","input":"hi"}`)
	converted, err := ConvertRequest(body, "grok-4.6", OperationResponses)
	if err != nil {
		t.Fatalf("转换失败: %v", err)
	}
	// responses 操作只替换 model，不注入
	var out map[string]json.RawMessage
	if err := json.Unmarshal(converted, &out); err != nil {
		t.Fatalf("输出非法 JSON: %v", err)
	}
	var input string
	if err := json.Unmarshal(out["input"], &input); err != nil {
		t.Fatalf("responses input 应为纯字符串 hi，实际 %#v", out["input"])
	}
	if input != "hi" {
		t.Fatalf("responses 不应注入，实际 input=%q", input)
	}
}
