package llm

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// Message is a single entry in a chat conversation.
type Message struct {
	Role       string     `json:"role"`
	Content    *string    `json:"content"`           // null for assistant tool-call turns
	ToolCalls  []ToolCall `json:"tool_calls,omitempty"`
	ToolCallID string     `json:"tool_call_id,omitempty"`
}

// ToolCall is a function call emitted by the model.
type ToolCall struct {
	ID       string       `json:"id"`
	Type     string       `json:"type"`
	Function FunctionCall `json:"function"`
}

// FunctionCall holds the name and JSON-encoded arguments of a tool call.
type FunctionCall struct {
	Name      string `json:"name"`
	Arguments string `json:"arguments"` // JSON string
}

// ToolDefinition describes a callable tool to the LLM.
type ToolDefinition struct {
	Type     string         `json:"type"`
	Function ToolFunctionDef `json:"function"`
}

// ToolFunctionDef is the OpenAI function object inside a tool definition.
type ToolFunctionDef struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	Parameters  json.RawMessage `json:"parameters"`
}

// CompletionResponse is a trimmed OpenAI-compatible chat completion response.
type CompletionResponse struct {
	Choices []struct {
		Message      Message `json:"message"`
		FinishReason string  `json:"finish_reason"`
	} `json:"choices"`
}

// Client is an OpenAI-compatible HTTP client for vLLM.
type Client struct {
	baseURL string
	model   string
	http    *http.Client
}

func NewClient(baseURL, model string) *Client {
	return &Client{
		baseURL: baseURL,
		model:   model,
		http:    &http.Client{Timeout: 120 * time.Second},
	}
}

// Complete sends a chat completion request to the vLLM endpoint.
func (c *Client) Complete(ctx context.Context, messages []Message, tools []ToolDefinition) (*CompletionResponse, error) {
	body := map[string]any{
		"model":       c.model,
		"messages":    messages,
		"max_tokens":  512,
		"temperature": 0.1,
		// Disable Qwen3 chain-of-thought thinking tokens; without this the model
		// consumes its entire max_tokens budget on <think>…</think> before the
		// tool call JSON, causing finish_reason:"length" and empty tool_calls.
		"chat_template_kwargs": map[string]any{"enable_thinking": false},
	}
	if len(tools) > 0 {
		body["tools"] = tools
		body["tool_choice"] = "auto"
	}

	payload, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		c.baseURL+"/v1/chat/completions", bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("vllm complete: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("vllm returned HTTP %d: %s", resp.StatusCode, string(body))
	}

	var out CompletionResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, fmt.Errorf("decode completion: %w", err)
	}
	if len(out.Choices) == 0 {
		return nil, fmt.Errorf("vllm returned no choices")
	}
	return &out, nil
}

// ChatRequest is the OpenAI-format request body used by the RCA engine.
type ChatRequest struct {
	Model       string    `json:"model"`
	Messages    []Message `json:"messages"`
	Temperature float64   `json:"temperature,omitempty"`
	MaxTokens   int       `json:"max_tokens,omitempty"`
}

// callRaw posts a raw JSON body to the given path and returns the raw response bytes.
// Used by the RCA engine which builds its own request body.
func (c *Client) callRaw(ctx context.Context, path string, body io.Reader) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("vllm callRaw: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("vllm HTTP %d: %s", resp.StatusCode, string(b))
	}
	return io.ReadAll(resp.Body)
}

// UserMessage constructs a user-role message.
func UserMessage(content string) Message { return Message{Role: "user", Content: &content} }

// SystemMessage constructs a system-role message.
func SystemMessage(content string) Message { return Message{Role: "system", Content: &content} }

// ToolResultMessage constructs the message that delivers a tool result back to the model.
func ToolResultMessage(callID, result string) Message {
	return Message{Role: "tool", ToolCallID: callID, Content: &result}
}
