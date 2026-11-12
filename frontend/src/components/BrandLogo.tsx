import { IMAGOTIPO, ISOTIPO, type LogoData } from "./BrandLogo.paths";

interface Props {
  variant?: "imagotipo" | "isotipo";
  /** Color del isotipo (las líneas). Por defecto: currentColor. */
  isoColor?: string;
  className?: string;
  title?: string;
}

/** Logotipo oficial de PROSIC (vectores extraídos del manual de recursos web). */
export function BrandLogo({ variant = "imagotipo", isoColor, className, title = "PROSIC" }: Props) {
  const data: LogoData = variant === "isotipo" ? ISOTIPO : IMAGOTIPO;
  return (
    <svg viewBox={`0 0 ${data.width} ${data.height}`} className={className} role="img" aria-label={title}>
      {data.paths.map((p, i) => (
        <path
          key={i}
          d={p.d}
          fillRule={p.evenodd ? "evenodd" : undefined}
          fill={isoColor && (variant === "isotipo" || p.iso) ? isoColor : "currentColor"}
        />
      ))}
    </svg>
  );
}
