import type { ChatMessage, ModuleKpis } from "../types/api";
import { apiFetch } from "./client";

export interface ChatHandlers {
  onToken: (text: string) => void;
  onToolStart: (name: string, args: Record<string, unknown>) => void;
  onToolResult: (name: string, data: ModuleKpis | null, error: string | null) => void;
  onError: (message: string) => void;
  onDone: () => void;
}

/** POST /api/ai/chat leyendo el stream SSE con fetch (EventSource no permite POST ni headers). */
export async function streamChat(messages: ChatMessage[], h: ChatHandlers, signal?: AbortSignal): Promise<void> {
  const res = await apiFetch("/api/ai/chat", { method: "POST", body: JSON.stringify({ messages }), signal });
  if (!res.body) throw new Error("El navegador no soporta streaming");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finished = false;

  const dispatch = (block: string) => {
    let event = "message";
    const dataLines: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith(":")) continue; // heartbeat
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    }
    if (!dataLines.length) return;
    const data = JSON.parse(dataLines.join("\n"));
    switch (event) {
      case "token":
        h.onToken(data.text);
        break;
      case "tool_start":
        h.onToolStart(data.name, data.args ?? {});
        break;
      case "tool_result":
        h.onToolResult(data.name, data.data ?? null, data.error ?? null);
        break;
      case "error":
        h.onError(data.message);
        break;
      case "done":
        finished = true;
        h.onDone();
        break;
    }
  };

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      dispatch(buffer.slice(0, idx));
      buffer = buffer.slice(idx + 2);
    }
  }
  if (buffer.trim()) dispatch(buffer);
  if (!finished) h.onDone();
}
