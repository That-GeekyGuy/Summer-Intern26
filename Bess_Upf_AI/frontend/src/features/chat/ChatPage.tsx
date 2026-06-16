import { useState, useEffect, useRef } from "react";
import { Credentials } from "../../api/client";
import { useChat, ChatMessage } from "../../hooks/useChat";
import { useAppStore } from "../../store/useAppStore";
import { COPY } from "../../lib/copy";

interface Props { creds: Credentials; }

export function ChatPage({ creds }: Props) {
  const { messages, loading, error, sendMessage, clearMessages } = useChat(creds);
  const [input, setInput] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const { prefillChat, setPrefillChat, setContext } = useAppStore();

  // Accept pre-fills from anomaly clicks
  useEffect(() => {
    if (prefillChat) {
      setInput(prefillChat);
      setPrefillChat("");
      textareaRef.current?.focus();
    }
  }, [prefillChat, setPrefillChat]);

  // Update context panel with latest reasoning trail
  useEffect(() => {
    const last = messages.filter(m => m.role === "assistant").at(-1);
    if (last?.details) {
      setContext({
        type: "chat",
        queriesUsed: last.details.queries_used,
        anomalyCount: last.details.anomaly_count,
      });
    }
  }, [messages, setContext]);

  // Auto-scroll
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  // Auto-grow textarea
  useEffect(() => {
    const ta = textareaRef.current;
    if (ta) {
      ta.style.height = "auto";
      ta.style.height = Math.min(ta.scrollHeight, 5 * 24) + "px";
    }
  }, [input]);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const text = input.trim();
    if (!text || loading) return;
    setInput("");
    sendMessage(text);
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e as unknown as React.FormEvent);
    }
  }

  const suggestedGroups = COPY.chat.groups;

  return (
    <div style={{ display: "flex", height: "100%", overflow: "hidden" }}>
      {/* Conversation panel — 65% */}
      <div style={{ flex: "0 0 65%", display: "flex", flexDirection: "column", overflow: "hidden", borderRight: "1px solid var(--border)" }}>
        {/* Header */}
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          padding: "10px 16px", borderBottom: "1px solid var(--border)", flexShrink: 0,
        }}>
          <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-sm)", fontWeight: 600 }}>
            {COPY.chat.title}
          </span>
          {messages.length > 0 && (
            <button
              onClick={clearMessages}
              style={{
                background: "none", border: "1px solid var(--border)", borderRadius: "var(--radius)",
                color: "var(--text-muted)", padding: "3px 10px", fontSize: "var(--text-xs)",
                cursor: "pointer", fontFamily: "var(--font-body)",
              }}
            >
              {COPY.chat.clearBtn}
            </button>
          )}
        </div>

        {/* Messages */}
        <div style={{ flex: 1, overflowY: "auto", padding: "16px", display: "flex", flexDirection: "column", gap: 16 }}>
          {messages.length === 0 && !loading && (
            <div style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)", textAlign: "center", padding: 24 }}>
              Start a conversation below, or pick a suggested question →
            </div>
          )}
          {messages.map(msg => (
            <MessageBubble key={msg.id} message={msg} />
          ))}
          {loading && (
            <div style={{ display: "flex", gap: 6, padding: "10px 0", alignItems: "center" }}>
              <span className="loading-dot" />
              <span className="loading-dot" />
              <span className="loading-dot" />
              <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", marginLeft: 4 }}>
                {COPY.chat.loadingMsg}
              </span>
            </div>
          )}
          {error && (
            <div style={{
              background: "rgba(255,77,77,0.08)",
              border: "1px solid var(--signal-critical)",
              borderRadius: "var(--radius)",
              padding: "10px 14px",
              fontSize: "var(--text-sm)",
              color: "var(--signal-critical)",
            }}>
              {COPY.chat.errorPrefix}
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        {/* Input */}
        <form onSubmit={handleSubmit} style={{
          padding: "12px 16px", borderTop: "1px solid var(--border)",
          flexShrink: 0, display: "flex", flexDirection: "column", gap: 8,
        }}>
          <div style={{ display: "flex", gap: 8 }}>
            <textarea
              ref={textareaRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={COPY.chat.placeholder}
              disabled={loading}
              rows={1}
              style={{
                flex: 1, resize: "none",
                background: "var(--bg-subtle)", border: "1px solid var(--border)",
                borderRadius: "var(--radius)", padding: "8px 12px",
                color: "var(--text-primary)", fontSize: "var(--text-sm)",
                fontFamily: "var(--font-body)", outline: "none",
                lineHeight: "24px", overflow: "hidden",
              }}
            />
            <button
              type="submit"
              disabled={loading || !input.trim()}
              style={{
                padding: "8px 20px", background: "var(--accent)", border: "none",
                borderRadius: "var(--radius)", color: "#fff", fontSize: "var(--text-sm)",
                fontFamily: "var(--font-body)", cursor: "pointer",
                opacity: loading || !input.trim() ? 0.5 : 1,
                alignSelf: "flex-end", fontWeight: 500,
              }}
            >
              {COPY.chat.send}
            </button>
          </div>
        </form>
      </div>

      {/* Suggested questions / reasoning rail — 35% */}
      <div style={{ flex: "0 0 35%", overflowY: "auto", padding: 16, display: "flex", flexDirection: "column", gap: 16 }}>
        {messages.length === 0 ? (
          <>
            <div style={{
              fontSize: "var(--text-xs)", color: "var(--text-muted)",
              fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.5px",
            }}>
              {COPY.chat.suggestedTitle}
            </div>
            {suggestedGroups.map((group) => (
              <div key={group.label} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <div style={{ fontSize: "var(--text-xs)", color: "var(--text-secondary)", fontWeight: 500, marginBottom: 2 }}>
                  {group.label}
                </div>
                {group.questions.map(q => (
                  <button
                    key={q}
                    onClick={() => setInput(q)}
                    style={{
                      textAlign: "left", background: "var(--bg-elevated)",
                      border: "1px solid var(--border)", borderRadius: "var(--radius)",
                      padding: "8px 12px", fontSize: "var(--text-sm)",
                      color: "var(--text-secondary)", cursor: "pointer",
                      lineHeight: 1.5, fontFamily: "var(--font-body)",
                    }}
                    onMouseEnter={ev => {
                      ev.currentTarget.style.borderColor = "var(--accent)";
                      ev.currentTarget.style.color = "var(--text-primary)";
                    }}
                    onMouseLeave={ev => {
                      ev.currentTarget.style.borderColor = "var(--border)";
                      ev.currentTarget.style.color = "var(--text-secondary)";
                    }}
                  >
                    {q}
                  </button>
                ))}
              </div>
            ))}
          </>
        ) : (
          <>
            <div style={{
              fontSize: "var(--text-xs)", color: "var(--text-muted)",
              fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.5px",
            }}>
              Reasoning trail
            </div>
            <p style={{ fontSize: "var(--text-sm)", color: "var(--text-muted)" }}>
              See Context panel for query details
            </p>
          </>
        )}
      </div>
    </div>
  );
}

// ── Message bubble ────────────────────────────────────────────────────────────

function MessageBubble({ message }: { message: ChatMessage }) {
  const [showReasoning, setShowReasoning] = useState(false);
  const isUser = message.role === "user";

  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: isUser ? "flex-end" : "flex-start", gap: 4 }}>
      <div style={{
        maxWidth: "78%",
        padding: "10px 14px",
        borderRadius: "var(--radius)",
        background: isUser ? "var(--bg-elevated)" : "none",
        border: isUser ? "1px solid var(--border)" : "none",
        borderLeft: isUser ? undefined : "2px solid var(--border)",
        paddingLeft: isUser ? undefined : 12,
        fontSize: "var(--text-sm)",
        lineHeight: 1.6,
        color: "var(--text-primary)",
        whiteSpace: "pre-wrap",
      }}>
        {message.content}
      </div>
      {!isUser && message.details && (
        <div style={{ paddingLeft: 16, maxWidth: "78%" }}>
          <button
            onClick={() => setShowReasoning(v => !v)}
            style={{
              background: "none", border: "none", color: "var(--text-muted)",
              fontSize: "var(--text-xs)", cursor: "pointer", fontFamily: "var(--font-body)",
              padding: "2px 0", display: "flex", alignItems: "center", gap: 6,
            }}
          >
            {showReasoning ? COPY.chat.reasoningHide : COPY.chat.reasoningShow} {COPY.chat.reasoningLbl}
            <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-2xs)", color: "var(--text-muted)" }}>
              {message.details.queries_used.length} quer{message.details.queries_used.length === 1 ? "y" : "ies"} · {message.details.anomaly_count} event{message.details.anomaly_count !== 1 ? "s" : ""}
            </span>
          </button>
          {showReasoning && (
            <div style={{
              marginTop: 6,
              background: "var(--bg-subtle)", border: "1px solid var(--border)",
              borderRadius: "var(--radius)", padding: "10px 12px",
              display: "flex", flexDirection: "column", gap: 8,
            }}>
              {message.details.queries_used.length > 0 && (
                <div>
                  <div style={{
                    fontSize: "var(--text-2xs)", color: "var(--text-muted)",
                    textTransform: "uppercase", letterSpacing: "0.5px",
                    marginBottom: 4, fontFamily: "var(--font-mono)",
                  }}>
                    PromQL
                  </div>
                  {message.details.queries_used.map((q, i) => (
                    <code key={i} style={{
                      display: "block", fontFamily: "var(--font-mono)",
                      fontSize: "var(--text-2xs)", color: "var(--text-secondary)",
                      wordBreak: "break-all", marginBottom: 2,
                    }}>
                      {q}
                    </code>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
