// One stable colour per provider, in configuration order (CSS variables in styles.css).
const SLOTS = 6;

export function providerColorVar(name: string | null, order: string[]): string {
  if (!name) return "var(--muted)";
  const i = order.indexOf(name);
  return `var(--cloud-${(i < 0 ? 0 : i) % SLOTS})`;
}

export function shortName(name: string): string {
  return name.replace(/^minio-/, "");
}

export function flag(jurisdiction: string): string {
  return jurisdiction === "IN" ? "🇮🇳" : "🌍";
}
