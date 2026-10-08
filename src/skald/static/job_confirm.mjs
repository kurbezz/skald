// Statuses for which organized library files may exist and will be removed on delete.
const LIBRARY_STATUSES = new Set(["organizing", "organized", "needs_attention", "deleting"]);

export function deleteConfirmText(title, status) {
  const removes = LIBRARY_STATUSES.has(status)
    ? "the torrent, downloaded files, and organized library files"
    : "the torrent and downloaded files";
  return `Delete “${title}”? This permanently removes ${removes}. This cannot be undone.`;
}
