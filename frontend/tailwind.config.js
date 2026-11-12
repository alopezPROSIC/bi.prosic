/** @type {import('tailwindcss').Config} */
// Los colores apuntan a variables CSS (src/index.css) para que el tema claro/oscuro
// se cambie en un solo lugar.
const v = (name) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        page: v("page"),
        surface: v("surface"),
        raised: v("raised"),
        ink: v("ink"),
        "ink-2": v("ink-2"),
        muted: v("muted"),
        line: v("line"),
        brand: v("brand"),
        "brand-ink": v("brand-ink"),
        good: v("good"),
        warn: v("warn"),
        bad: v("bad"),
        accent: v("accent"),
        "accent-ink": v("accent-ink"),
        tint: v("tint"),
        side: v("side"),
        "side-ink": v("side-ink"),
        "side-muted": v("side-muted"),
        "side-line": v("side-line"),
      },
      fontFamily: {
        // Tipografía digital de PROSIC (empaquetada con la app: no se carga de Google Fonts)
        sans: ['"Figtree Variable"', "Figtree", "system-ui", "-apple-system", '"Segoe UI"', "Roboto", "sans-serif"],
      },
    },
  },
  plugins: [],
};
