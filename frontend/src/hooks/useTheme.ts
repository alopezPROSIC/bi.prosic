import { useCallback, useEffect, useState } from "react";

export type Theme = "light" | "dark";

function current(): Theme {
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

/** Tema claro/oscuro. Notifica a todos los suscriptores (gráficas) cuando cambia. */
export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(current);

  useEffect(() => {
    const obs = new MutationObserver(() => setTheme(current()));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => obs.disconnect();
  }, []);

  const toggle = useCallback(() => {
    const next: Theme = current() === "dark" ? "light" : "dark";
    document.documentElement.classList.toggle("dark", next === "dark");
    try {
      localStorage.setItem("prosic-theme", next);
    } catch {
      /* ignorar */
    }
  }, []);

  return [theme, toggle];
}
