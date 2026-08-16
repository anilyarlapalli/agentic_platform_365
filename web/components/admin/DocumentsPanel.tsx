"use client";

import { useRef, useState } from "react";
import {
  ALLOWED_SUFFIXES,
  Cap,
  MAX_UPLOAD_BYTES,
  can,
  deleteDocument,
  fetchDocuments,
  uploadDocument,
  type Me,
} from "@/lib/api";
import { Denied, Empty, ErrorNote, Panel, Spinner, Table, useResource } from "./Panel";

function humanBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function DocumentsPanel({ me }: { me: Me }) {
  const mayRead = can(me, Cap.DOCUMENT_READ);
  const mayIngest = can(me, Cap.DOCUMENT_INGEST);
  const mayDelete = can(me, Cap.DOCUMENT_DELETE);

  const [filter, setFilter] = useState("");
  const { data, error, loading, reload } = useResource(
    () => fetchDocuments(filter || undefined),
    [filter],
    mayRead,
  );

  const [collection, setCollection] = useState("maintenance");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<unknown>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const upload = async (file: File) => {
    setActionError(null);

    // Validated here as well as at the API. The server is the authority, but
    // rejecting locally avoids base64-encoding 50MB just to be refused.
    const dot = file.name.lastIndexOf(".");
    const suffix = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
    if (!ALLOWED_SUFFIXES.includes(suffix)) {
      setActionError(new Error(`Unsupported file type "${suffix}". Allowed: ${ALLOWED_SUFFIXES.join(", ")}`));
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setActionError(new Error(`File is ${humanBytes(file.size)}; the limit is ${humanBytes(MAX_UPLOAD_BYTES)}.`));
      return;
    }

    setBusy(true);
    try {
      await uploadDocument(collection, file);
      if (fileRef.current) fileRef.current.value = "";
      await reload();
    } catch (err) {
      setActionError(err);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string, filename: string) => {
    if (!window.confirm(`Delete "${filename}"? This cannot be undone.`)) return;
    setBusy(true);
    setActionError(null);
    try {
      await deleteDocument(id);
      await reload();
    } catch (err) {
      setActionError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel
      title="Documents"
      subtitle="Deduplicated by content hash within a collection, so a double-clicked upload is one document rather than two rebuilds racing."
      actions={
        mayRead ? (
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="filter by collection"
            className="input w-44 py-1 text-[12px]"
          />
        ) : null
      }
    >
      {!mayRead ? (
        <Denied capability={Cap.DOCUMENT_READ} />
      ) : (
        <>
          {mayIngest ? (
            <div className="mb-3 flex flex-wrap items-end gap-2 rounded-xl border border-cream-300 bg-cream-50/60 p-2.5">
              <label>
                <span className="label">Collection</span>
                <input
                  value={collection}
                  onChange={(e) => setCollection(e.target.value)}
                  pattern="[a-z0-9][a-z0-9_-]*"
                  title="lowercase letters, digits, underscore and hyphen; must not start with a separator"
                  className="input w-44 font-mono"
                />
              </label>
              <label className="flex-1">
                <span className="label">File</span>
                <input
                  ref={fileRef}
                  type="file"
                  accept={ALLOWED_SUFFIXES.join(",")}
                  disabled={busy}
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) void upload(f);
                  }}
                  className="block w-full text-[12px] text-ink-600 file:mr-2 file:rounded-lg file:border-0 file:bg-copper-600 file:px-3 file:py-1.5 file:text-[12px] file:font-medium file:text-white hover:file:bg-copper-700 disabled:opacity-50"
                />
              </label>
              {busy ? <span className="text-[12px] text-ink-400">Working…</span> : null}
            </div>
          ) : (
            <div className="mb-3">
              <Denied capability={Cap.DOCUMENT_INGEST} />
            </div>
          )}

          {actionError ? (
            <div className="mb-2">
              <ErrorNote error={actionError} />
            </div>
          ) : null}

          {loading ? (
            <Spinner />
          ) : error ? (
            <ErrorNote error={error} />
          ) : !data || data.documents.length === 0 ? (
            <Empty>No documents{filter ? ` in "${filter}"` : ""} yet.</Empty>
          ) : (
            <Table head={["filename", "collection", "size", "sha256", "created", ""]}>
              {data.documents.map((d) => (
                <tr key={d.id} className="border-b border-cream-200 last:border-0">
                  <td className="px-2 py-1.5 text-ink-800">{d.filename}</td>
                  <td className="px-2 py-1.5">
                    <code className="font-mono text-[11.5px] text-ink-600">
                      {d.collection}
                    </code>
                  </td>
                  <td className="tnum px-2 py-1.5 text-ink-600">
                    {humanBytes(d.byte_size)}
                  </td>
                  <td className="px-2 py-1.5">
                    <code
                      className="font-mono text-[11px] text-ink-400"
                      title={d.content_sha256}
                    >
                      {d.content_sha256.slice(0, 12)}
                    </code>
                  </td>
                  <td className="tnum px-2 py-1.5 text-[11.5px] text-ink-500">
                    {new Date(d.created_at).toLocaleString()}
                  </td>
                  <td className="px-2 py-1.5 text-right">
                    {mayDelete ? (
                      <button
                        type="button"
                        onClick={() => void remove(d.id, d.filename)}
                        disabled={busy}
                        className="btn-ghost px-2 py-1 text-[11.5px] text-danger"
                      >
                        Delete
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </Table>
          )}
        </>
      )}
    </Panel>
  );
}
