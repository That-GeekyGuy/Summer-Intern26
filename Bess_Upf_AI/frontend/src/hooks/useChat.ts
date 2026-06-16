import { useState, useCallback } from "react";
import {
  sendChatMessage,
  ChatResponse,
  Credentials,
} from "../api/client";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  details?: {
    queries_used: string[];
    anomaly_count: number;
    raw: ChatResponse;
  };
}

const SESSION_KEY = "upf_monitor_session_id";

function getSessionId(): string | undefined {
  return sessionStorage.getItem(SESSION_KEY) || undefined;
}

function saveSessionId(id: string): void {
  sessionStorage.setItem(SESSION_KEY, id);
}

export function useChat(creds: Credentials | null) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const sendMessage = useCallback(
    async (text: string) => {
      if (!creds || !text.trim()) return;

      const userMsg: ChatMessage = {
        id: crypto.randomUUID(),
        role: "user",
        content: text.trim(),
      };
      setMessages((prev) => [...prev, userMsg]);
      setLoading(true);
      setError(null);

      try {
        const sessionId = getSessionId();
        const resp = await sendChatMessage(creds, text.trim(), sessionId);
        saveSessionId(resp.session_id);

        const assistantMsg: ChatMessage = {
          id: crypto.randomUUID(),
          role: "assistant",
          content: resp.answer,
          details: {
            queries_used: resp.queries_used ?? [],
            anomaly_count: resp.anomaly_count ?? 0,
            raw: resp,
          },
        };
        setMessages((prev) => [...prev, assistantMsg]);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Unknown error");
      } finally {
        setLoading(false);
      }
    },
    [creds]
  );

  const clearMessages = useCallback(() => {
    setMessages([]);
    setError(null);
  }, []);

  return { messages, loading, error, sendMessage, clearMessages };
}
