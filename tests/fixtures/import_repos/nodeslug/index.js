export default function slugify(text, options = {}) {
  const sep = options.separator ?? "-";
  return String(text).toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9]+/g, sep).replace(new RegExp(`^${sep}|${sep}$`, "g"), "");
}
export function helper() { return 1; }
