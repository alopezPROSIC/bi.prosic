import type { ReactNode } from "react";
import { IconAlert, IconInfo } from "./Icons";

export function CardsSkeleton({ n = 6 }: { n?: number }) {
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6" aria-busy="true" aria-label="Cargando indicadores">
      {Array.from({ length: n }, (_, i) => (
        <div key={i} className="card space-y-3 p-4">
          <div className="skeleton h-3 w-2/3" />
          <div className="skeleton h-7 w-1/2" />
          <div className="skeleton h-3 w-1/3" />
        </div>
      ))}
    </div>
  );
}

export function ChartSkeleton() {
  return (
    <div className="card p-4" aria-busy="true">
      <div className="skeleton mb-4 h-4 w-1/3" />
      <div className="skeleton h-56 w-full" />
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="card flex flex-col items-start gap-3 border-bad/40 p-5">
      <div className="flex items-center gap-2 font-medium text-bad">
        <IconAlert /> No se pudieron cargar los datos
      </div>
      <p className="text-sm text-ink-2">{message}</p>
      {onRetry && (
        <button className="btn-ghost border border-line" onClick={onRetry}>
          Reintentar
        </button>
      )}
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="card flex flex-col items-center gap-2 p-8 text-center">
      <IconInfo className="text-muted" width={28} height={28} />
      <p className="font-medium">{title}</p>
      {children && <div className="max-w-md text-sm text-ink-2">{children}</div>}
    </div>
  );
}

export function Notes({ notes }: { notes: string[] }) {
  if (!notes.length) return null;
  return (
    <details className="card p-3 text-sm text-ink-2">
      <summary className="cursor-pointer select-none font-medium text-ink">
        Notas sobre los datos ({notes.length})
      </summary>
      <ul className="mt-2 list-disc space-y-1 pl-5">
        {notes.map((n) => (
          <li key={n}>{n}</li>
        ))}
      </ul>
    </details>
  );
}
