# Streaming / disable_streaming — Debug Plan

## Current state

- A new provider option `disable_streaming` was added (config + schema + docs).
- Providers expose a `StreamResponse(ctx, messages, tools) <-chan ProviderEvent` API. When `disable_streaming` is true, the base provider falls back to calling `send` and emitting a single `EventComplete` on a returned channel (so code using a stream-like API still works).
- The agent previously ranged over the provider event channel and waited for the channel to close. Some provider implementations (or certain fallback paths) can emit a final `EventComplete` but leave the channel open (or not close it promptly), so the agent could hang waiting for the channel to close.
- I changed the agent to break out of the event loop when it observes `provider.EventComplete`, so the agent no longer blocks waiting for the provider channel to close. Provider tests passed locally after this change.

## Root cause

- Agent code used `for event := range eventChan { ... }` and relied on the provider to close the channel to cease waiting.
- Certain provider code paths emit a final `EventComplete` but do not close the channel promptly (or leave it open), so the agent could remain blocked even after completion.

## Important files (what to read first)

- `internal/llm/provider/provider.go` — defines `Provider`, `ProviderEvent`, `ProviderResponse`, and the `baseProvider` fallback behavior for `disable_streaming`.
- `internal/config/config.go` + `schema.json` — added `disable_streaming` to provider config.
- `internal/llm/provider/*` (openai.go, anthropic.go, gemini.go, bedrock.go, azure.go) — provider client implementations and stream/send implementations.
- `internal/llm/provider/provider_disable_streaming_test.go` — unit tests for the base provider fallback behavior.
- `internal/llm/agent/agent.go` — agent implementation, `streamAndHandleEvents` and `processEvent` are the relevant functions. The agent previously ranged over the provider event channel and waited for it to close; now it breaks on `EventComplete`.

## Recommended tests (goal)

1. Integration-style test that simulates a provider that emits a final `EventComplete` and then DOES NOT close its event channel. The test must assert that the agent (or the stream handling path) does not hang and can proceed to handle tool calls / finish the message.
2. Agent-level unit test that verifies `streamAndHandleEvents` returns when the provider emits `EventComplete`, even if the provider channel remains open.

These tests ensure the agent's new behavior won't regress and that we won't hang when providers/clients emit a final completion event but leave streams open.

## Exact test plan and implementation steps

Note: after you restart the session with sandbox off I'll implement these. Below are precise steps and test skeletons.

1) Add integration test for provider + agent interaction

  - File: `internal/llm/agent/agent_eventcomplete_no_close_test.go`
  - Purpose: build a minimal test harness that exercises `streamAndHandleEvents` using a fake provider whose `StreamResponse` sends one `EventComplete` and then blocks (doesn't close channel). Ensure `streamAndHandleEvents` returns within a timeout.
  - Strategy:
    - Create a `fakeProvider` implementing `provider.Provider` with `SendMessages` and `StreamResponse`.
    - `StreamResponse` returns a channel and a goroutine that sends a single `ProviderEvent{Type: provider.EventComplete, Response: &ProviderResponse{...}}` and then intentionally does NOT close the channel (or blocks after sending).
    - Create minimal mocks for `message.Service` and `session.Service` (or use small in-memory implementations from the codebase if available). The agent needs to create an assistant message and update it; the mock should record updates and provide a `ToolCalls()` view.
    - Construct an `agent` instance by populating the struct fields directly (avoid `NewAgent` complexity). Fill required fields: `messages` mock, `sessions` mock, `provider` as `fakeProvider`, `providerID`, `activeRequests` map, `tools` minimal slice, and `agentCfg` model info as needed.
    - Call `a.streamAndHandleEvents(ctx, sessionID, msgHistory)` with a context that has a timeout (e.g., 3s). Verify the call returns before the timeout and that the returned assistant message has the expected finish reason and content.

  - Test skeleton (pseudocode):

```go
func TestAgent_StreamAndHandleEvents_EventCompleteNoClose(t *testing.T) {
    t.Parallel()
    // fake provider that sends EventComplete then never closes channel
    fp := &fakeProvider{...}

    // minimal messages and sessions mocks
    msgsSvc := NewInMemoryMessages() // or implement mock
    sessSvc := NewInMemorySessions()

    a := &agent{
        Broker: pubsub.NewBroker[AgentEvent](),
        agentCfg: config.Agent{...},
        messages: msgsSvc,
        sessions: sessSvc,
        provider: fp,
        providerID: "fake",
        activeRequests: csync.NewMap[string, context.CancelFunc](),
        tools: csync.NewLazySlice(func() []tools.BaseTool { return nil }),
    }

    ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
    defer cancel()

    assistantMsg, toolResults, err := a.streamAndHandleEvents(ctx, "session-id", []message.Message{{Role: message.User, Parts: []message.ContentPart{message.TextContent{Text: "hi"}}}})
    if err != nil { t.Fatalf("unexpected error: %v", err) }
    // assertions: assistantMsg.FinishReason() is set, assistantMsg.Content contains expected content
}
```

  - Important: avoid relying on `range ch` in the test; use timeouts so the test can't hang indefinitely.

2) Add a direct agent unit test (finer-grained)

  - File: `internal/llm/agent/agent_stream_break_test.go`
  - Purpose: ensure the loop in `streamAndHandleEvents` breaks (via `EventComplete`) and continues to tool processing.
  - Strategy:
    - Use a fake provider client that yields multiple events: first a couple of `EventContentDelta` events, then one `EventComplete`, then never closes.
    - Same construction of `agent` as above, but assert that after `streamAndHandleEvents` returns the assistant message contains concatenated content from the deltas plus the final response content.

  - Test skeleton (pseudocode):

```go
func TestAgent_BreakOnEventComplete(t *testing.T) {
    // fake provider that sends content deltas then EventComplete and doesn't close
    fp := &fakeProvider{streamEvents: []provider.ProviderEvent{ {Type: provider.EventContentDelta, Content: "a"}, {Type: provider.EventContentDelta, Content:"b"}, {Type: provider.EventComplete, Response: &provider.ProviderResponse{Content: "final"}} }}
    // construct agent similar to integration test
    ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
    defer cancel()
    assistantMsg, _, err := a.streamAndHandleEvents(ctx, "s", history)
    if err != nil { t.Fatalf("unexpected: %v", err) }
    // expect assistantMsg.Content() to contain "ab" or "final" depending on processEvent merging behavior
}
```

3) Implementation notes & helper code

- Implement `fakeProvider` and `fakeClient` as small test-only types in `_test.go` files under the appropriate packages.
- Provide small in-memory implementations of `message.Service` and `session.Service` if the codebase doesn't already expose test helpers. Keep them minimal: implement only the methods the agent calls in `streamAndHandleEvents` and `createUserMessage` (Create, Update, List, Get, Save as needed).
- Use context timeouts liberally in tests to ensure they fail fast if something blocks.
- Mark tests `t.Parallel()` where appropriate.

4) Running tests

- Single-package provider tests:
  - `go test ./internal/llm/provider -v`
- Agent tests (once added):
  - `go test ./internal/llm/agent -run TestAgent_StreamAndHandleEvents_EventCompleteNoClose -v`
- Full test suite (CI):
  - `go test ./...` (note: earlier in this environment full test run hit MacOS sandbox build cache permission errors; CI should be used for a full run)

## Acceptance criteria

- New tests pass reliably and do not hang.
- The agent no longer blocks when a provider emits `EventComplete` but the provider does not close its stream channel.
- Coverage: at least one test that explicitly simulates `EventComplete` without closing the channel.

## After you restart the session

When you restart and disable sandbox I will:

1. Add the two test files and any minimal test helpers (fake provider, in-memory message/session services).
2. Run package tests (`go test ./internal/llm/provider` and `go test ./internal/llm/agent`).
3. Iterate until tests pass and add any needed adjustments.

If you want any additional assertions or specific provider/client behaviors simulated (e.g., tool call flows, thinking deltas), tell me and I'll include them in the test skeletons when I implement.

---
Generated: STREAMING_DEBUG_PLAN.md

