import { Fragment, type ReactNode } from "react";

/** Markdown mínimo y seguro (sin HTML): párrafos, viñetas, listas numeradas, **negritas** y encabezados. */
function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") ? <strong key={i}>{part.slice(2, -2)}</strong> : <Fragment key={i}>{part}</Fragment>,
  );
}

export function SimpleMarkdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;

  const flush = () => {
    if (!list) return;
    const items = list.items.map((it, i) => <li key={i}>{inline(it)}</li>);
    blocks.push(
      list.ordered ? (
        <ol key={blocks.length} className="list-decimal space-y-1 pl-5">{items}</ol>
      ) : (
        <ul key={blocks.length} className="list-disc space-y-1 pl-5">{items}</ul>
      ),
    );
    list = null;
  };

  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || numbered) {
      const ordered = Boolean(numbered);
      if (!list || list.ordered !== ordered) {
        flush();
        list = { ordered, items: [] };
      }
      list.items.push((bullet ?? numbered)![1]);
      continue;
    }
    flush();
    if (!line.trim()) continue;
    const heading = line.match(/^#{1,4}\s+(.*)$/);
    blocks.push(
      heading ? (
        <p key={blocks.length} className="font-semibold">{inline(heading[1])}</p>
      ) : (
        <p key={blocks.length}>{inline(line)}</p>
      ),
    );
  }
  flush();
  return <div className="space-y-2 leading-relaxed">{blocks}</div>;
}
