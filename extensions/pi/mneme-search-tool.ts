// Mneme search_memory tool for Pi
// Docs: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "search_memory",
    label: "Search Memory",
    description:
      "Search the Mneme persistent memory database for past conversations. " +
      "Use this when injected memory chunks don't contain enough detail. " +
      "Returns chunk IDs, topic labels, grades, and message content.",
    promptSnippet: "Search the Mneme persistent memory database for past conversations",
    promptGuidelines: [
      "Use search_memory when you need details from past conversations that are not in the injected memory chunks.",
    ],
    parameters: Type.Object({
      query: Type.String({
        description: "Search terms, e.g. 'Sean Portland dog name'",
      }),
      top_k: Type.Optional(
        Type.Number({
          default: 5,
          description: "Number of results to return",
        })
      ),
    }),

    async execute(_toolCallId, params, signal, onUpdate) {
      const query = params.query as string;
      const top_k = (params.top_k as number | undefined) ?? 5;

      onUpdate?.({
        content: [{ type: "text", text: `Searching memory for: ${query}` }],
      });

      if (signal?.aborted) {
        return { content: [{ type: "text", text: "Cancelled" }], details: {} };
      }

      let res: Response;
      try {
        res = await fetch("http://localhost:8080/search", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query, top_k }),
          signal,
        });
      } catch (err: any) {
        throw new Error(`search_memory: fetch failed: ${err?.message ?? err}`);
      }

      if (!res.ok) {
        throw new Error(`search_memory failed: HTTP ${res.status}`);
      }

      const data = (await res.json()) as {
        results?: Array<{
          chunk_id?: string;
          grade?: string;
          topic_label?: string;
          messages?: string;
        }>;
      };

      if (!data.results || data.results.length === 0) {
        return {
          content: [{ type: "text", text: "No matching memories found." }],
          details: { query, count: 0 },
        };
      }

      const text = data.results
        .map(
          (r) =>
            `[${r.chunk_id ?? "?"} | G:${r.grade ?? "?"}] ${r.topic_label ?? ""}\n${r.messages ?? ""}`
        )
        .join("\n\n");

      return {
        content: [{ type: "text", text }],
        details: { query, count: data.results.length },
      };
    },
  });
}
