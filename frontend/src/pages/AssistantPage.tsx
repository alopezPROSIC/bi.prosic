import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { streamChat } from "../api/sse";
import { IconSend, IconSpark } from "../components/Icons";
import { ModuleView } from "../components/ModuleView";
import { SimpleMarkdown } from "../components/SimpleMarkdown";
import type { ChatMessage, ModuleKpis } from "../types/api";

interface ToolCall {
  name: string;
  status: "running" | "done" | "error";
  data: ModuleKpis | null;
  error: string | null;
}

interface UiMessage extends ChatMessage {
  tools: ToolCall[];
  error?: string;
}

const TOOL_LABELS: Record<string, string> = {
  get_ventas: "Consultando ventas",
  get_cxc: "Consultando cuentas por cobrar",
  get_utilizacion_rentas: "Consultando utilización de rentas",
  get_historico_flota: "Consultando histórico de ocupación",
  get_mantenimiento: "Consultando mantenimiento",
  get_compras: "Consultando compras",
  get_pronostico: "Calculando pronóstico y vencimientos",
};

const SUGGESTED = [
  "Dame un pronóstico de ventas, rentas y cobranza para los próximos 6 meses",
  "¿Cuál fue la ocupación promedio de la flota el mes pasado por almacén?",
  "¿Cuánto tenemos vencido a más de 90 días y con qué clientes?",
  "¿Cómo van las ventas de este mes contra el mes anterior?",
  "¿Qué equipos tuvieron más fallas correctivas en los últimos 12 meses?",
  "¿Qué tan concentradas están nuestras compras en pocos proveedores este año?",
];

export function AssistantPage() {
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const location = useLocation();
  const navigate = useNavigate();

  const patchLast = (fn: (m: UiMessage) => UiMessage) =>
    setMessages((prev) => (prev.length ? [...prev.slice(0, -1), fn(prev[prev.length - 1])] : prev));

  const send = useCallback(
    async (text: string) => {
      const q = text.trim();
      if (!q || busy) return;
      const history: ChatMessage[] = [
        ...messages.filter((m) => m.content.trim()).map(({ role, content }) => ({ role, content })),
        { role: "user", content: q },
      ];
      setMessages((prev) => [...prev, { role: "user", content: q, tools: [] }, { role: "assistant", content: "", tools: [] }]);
      setInput("");
      setBusy(true);
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      try {
        await streamChat(
          history.slice(-20),
          {
            onToken: (t) => patchLast((m) => ({ ...m, content: m.content + t })),
            onToolStart: (name) => patchLast((m) => ({ ...m, tools: [...m.tools, { name, status: "running", data: null, error: null }] })),
            onToolResult: (name, data, error) =>
              patchLast((m) => {
                const tools = [...m.tools];
                const idx = tools.findIndex((t) => t.name === name && t.status === "running");
                const done: ToolCall = { name, status: error ? "error" : "done", data, error };
                if (idx >= 0) tools[idx] = done;
                else tools.push(done);
                return { ...m, tools };
              }),
            onError: (msg) => patchLast((m) => ({ ...m, error: msg })),
            onDone: () => {},
          },
          ctrl.signal,
        );
      } catch (err) {
        if ((err as Error).name !== "AbortError") patchLast((m) => ({ ...m, error: (err as Error).message }));
      } finally {
        setBusy(false);
        abortRef.current = null;
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [busy, messages],
  );

  // Pregunta sugerida desde el panel de análisis de un módulo
  useEffect(() => {
    const q = (location.state as { question?: string } | null)?.question;
    if (q) {
      navigate(location.pathname, { replace: true, state: null });
      void send(q);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    void send(input);
  };

  return (
    <div className="mx-auto flex h-full max-w-5xl flex-col">
      <div className="flex-1 space-y-5 pb-4" aria-live="polite">
        {messages.length === 0 && (
          <div className="card p-6">
            <div className="mb-3 flex items-center gap-2 font-semibold">
              <IconSpark className="text-brand" /> Pregunta sobre ventas, cobranza, rentas, mantenimiento o compras
            </div>
            <p className="mb-4 text-sm text-ink-2">
              El asistente solo consulta indicadores predefinidos del backend (nunca accede a Odoo libremente) y los nombres de clientes y
              proveedores se anonimizan antes de enviarse a la IA.
            </p>
            <div className="flex flex-wrap gap-2">
              {SUGGESTED.map((q) => (
                <button key={q} className="rounded-full border border-line px-3 py-1.5 text-left text-sm text-ink-2 hover:border-brand hover:text-ink" onClick={() => send(q)}>
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m, i) =>
          m.role === "user" ? (
            <div key={i} className="flex justify-end">
              <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-brand px-4 py-2 text-sm text-brand-ink">{m.content}</div>
            </div>
          ) : (
            <div key={i} className="space-y-3">
              {m.tools.map((t, j) => (
                <div key={j} className="text-xs text-muted">
                  {t.status === "running" ? "⏳ " : t.status === "error" ? "⚠️ " : "✓ "}
                  {TOOL_LABELS[t.name] ?? t.name}
                  {t.error ? ` — ${t.error}` : ""}
                </div>
              ))}
              {(m.content || (busy && i === messages.length - 1)) && (
                <div className="card px-4 py-3 text-sm">
                  {m.content ? <SimpleMarkdown text={m.content} /> : <div className="skeleton h-3 w-1/3" aria-label="Escribiendo" />}
                </div>
              )}
              {m.error && (
                <p role="alert" className="text-sm text-bad">
                  {m.error}
                </p>
              )}
              {m.tools
                .filter((t) => t.data)
                .map((t, j) => (
                  <details key={`d${j}`} className="card p-3" open={j === 0 && i === messages.length - 1}>
                    <summary className="cursor-pointer select-none text-sm font-medium">Datos consultados: {TOOL_LABELS[t.name]?.replace("Consultando ", "") ?? t.name}</summary>
                    <div className="mt-3">
                      <ModuleView data={t.data!} compact />
                    </div>
                  </details>
                ))}
            </div>
          ),
        )}
        <div ref={bottomRef} />
      </div>

      <form onSubmit={submit} className="sticky bottom-0 flex gap-2 border-t border-line bg-page py-3">
        <input
          className="input flex-1 py-2"
          placeholder="Escribe tu pregunta…"
          value={input}
          maxLength={2000}
          onChange={(e) => setInput(e.target.value)}
          disabled={busy}
          aria-label="Pregunta para el asistente"
        />
        {busy ? (
          <button type="button" className="btn-ghost border border-line" onClick={() => abortRef.current?.abort()}>
            Detener
          </button>
        ) : (
          <button className="btn-primary" disabled={!input.trim()}>
            <IconSend width={16} height={16} /> Enviar
          </button>
        )}
      </form>
    </div>
  );
}
