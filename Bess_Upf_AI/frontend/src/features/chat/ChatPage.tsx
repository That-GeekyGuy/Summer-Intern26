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
    <div style={{ display: "flex", height: "100%", overflow: "hidden", padding: 16, gap: 16 }}>
      {/* Bento Container */}
      <div style={{
        display: "flex", flex: 1, overflow: "hidden",
        background: "var(--bg-surface)",
        borderRadius: "var(--radius)",
        border: "1px solid var(--border)",
        boxShadow: "var(--shadow-soft)",
      }}>
        {/* Conversation panel — 65% */}
        <div style={{ flex: "0 0 65%", display: "flex", flexDirection: "column", overflow: "hidden", borderRight: "1px solid var(--border)" }}>
          {/* Header */}
          <div style={{
            display: "flex", alignItems: "center", justifyContent: "space-between",
            padding: "16px 20px", borderBottom: "1px solid var(--border)", flexShrink: 0,
          }}>
            <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-base)", fontWeight: 600, color: "var(--text-primary)" }}>
              {COPY.chat.title}
            </span>
            {messages.length > 0 && (
              <button
                onClick={clearMessages}
                style={{
                  background: "var(--bg-elevated)", border: "1px solid var(--border)", borderRadius: 100,
                  color: "var(--text-secondary)", padding: "4px 12px", fontSize: "var(--text-xs)",
                  cursor: "pointer", fontFamily: "var(--font-body)", fontWeight: 500,
                  transition: "all 0.2s"
                }}
              >
                {COPY.chat.clearBtn}
              </button>
            )}
          </div>

          {/* Messages */}
          <div style={{ flex: 1, overflowY: "auto", padding: "24px", display: "flex", flexDirection: "column", gap: 24 }}>
            {messages.length === 0 && !loading && (
              <div style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)", textAlign: "center", padding: 48, background: "var(--bg-base)", borderRadius: 16, border: "1px dashed var(--border)" }}>
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
                padding: "12px 16px",
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
            padding: "16px 20px", borderTop: "1px solid var(--border)", background: "var(--bg-base)",
            flexShrink: 0, display: "flex", flexDirection: "column", gap: 8,
          }}>
            <div style={{ display: "flex", gap: 12 }}>
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
                  background: "var(--bg-surface)", border: "1px solid var(--border)",
                  borderRadius: 24, padding: "12px 16px",
                  color: "var(--text-primary)", fontSize: "var(--text-sm)",
                  fontFamily: "var(--font-body)", outline: "none",
                  lineHeight: "24px", overflow: "hidden",
                  boxShadow: "inset 0 2px 4px rgba(0,0,0,0.02)",
                }}
                onFocus={e => e.target.style.borderColor = "var(--border-focus)"}
                onBlur={e => e.target.style.borderColor = "var(--border)"}
              />
              <button
                type="submit"
                disabled={loading || !input.trim()}
                style={{
                  padding: "0 24px", background: "var(--accent)", border: "none",
                  borderRadius: 100, color: "var(--bg-base)", fontSize: "var(--text-sm)",
                  fontFamily: "var(--font-body)", cursor: "pointer",
                  opacity: loading || !input.trim() ? 0.5 : 1,
                  alignSelf: "stretch", fontWeight: 600,
                  boxShadow: "var(--shadow-soft)",
                }}
              >
                {COPY.chat.send}
              </button>
            </div>
          </form>
        </div>

        {/* Suggested questions / reasoning rail — 35% */}
        <div style={{ flex: "0 0 35%", overflowY: "auto", padding: 24, display: "flex", flexDirection: "column", gap: 24, background: "var(--bg-base)" }}>
          {messages.length === 0 ? (
            <>
              <div style={{
                fontSize: "var(--text-xs)", color: "var(--text-primary)",
                fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.5px", fontWeight: 600
              }}>
                {COPY.chat.suggestedTitle}
              </div>
              {suggestedGroups.map((group) => (
                <div key={group.label} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  <div style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)", fontWeight: 500, marginBottom: 4 }}>
                    {group.label}
                  </div>
                  {group.questions.map(q => (
                    <button
                      key={q}
                      onClick={() => setInput(q)}
                      style={{
                        textAlign: "left", background: "var(--bg-surface)",
                        border: "1px solid var(--border)", borderRadius: 12,
                        padding: "12px 16px", fontSize: "var(--text-sm)",
                        color: "var(--text-secondary)", cursor: "pointer",
                        lineHeight: 1.5, fontFamily: "var(--font-body)",
                        boxShadow: "var(--shadow-soft)", transition: "all 0.2s"
                      }}
                      onMouseEnter={ev => {
                        ev.currentTarget.style.borderColor = "var(--border-focus)";
                        ev.currentTarget.style.transform = "translateY(-1px)";
                        ev.currentTarget.style.color = "var(--text-primary)";
                      }}
                      onMouseLeave={ev => {
                        ev.currentTarget.style.borderColor = "var(--border)";
                        ev.currentTarget.style.transform = "none";
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
