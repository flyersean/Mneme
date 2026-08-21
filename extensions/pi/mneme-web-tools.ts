// Mneme web tools extension for Pi
// Adds web_search and web_scrape — no API keys needed.
// Uses DuckDuckGo for search, plain HTTP fetch for scraping.
//
// Usage: pi --extension /workspace/mneme-web-tools.ts

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

export default function (pi: ExtensionAPI) {
  
  // ── web_search ──
  pi.registerTool({
    name: "web_search",
    label: "Web Search",
    description:
      "Search the web using DuckDuckGo. Returns titles, URLs, and snippets. " +
      "Free, no API key required.",
    promptSnippet: "Search the web for information using DuckDuckGo",
    parameters: Type.Object({
      query: Type.String({ description: "What to search for" }),
      limit: Type.Optional(
        Type.Number({ default: 5, description: "Max results (default 5)" })
      ),
    }),

    async execute(_toolCallId, params, signal, onUpdate) {
      const query = params.query as string;
      const limit = (params.limit as number | undefined) ?? 5;

      onUpdate?.({
        content: [{ type: "text", text: `Searching web for: ${query}` }],
      });

      if (signal?.aborted) {
        return { content: [{ type: "text", text: "Cancelled" }], details: {} };
      }

      try {
        const url = `https://html.duckduckgo.com/html/?q=${encodeURIComponent(query)}`;
        const resp = await fetch(url, {
          headers: { "User-Agent": "Mneme/1.0" },
          signal: AbortSignal.timeout(10000),
        });
        const html = await resp.text();
        
        // Extract result blocks. DDG changed the container class from
        // class="result" to class="result results_links ... web-result ", so
        // split on `class="result` followed by a space or quote to match both.
        const results: { title: string; url: string; snippet: string }[] = [];
        const blocks = html.split(/class="result[\s"]/);
        for (let i = 1; i < blocks.length && results.length < limit; i++) {
          const b = blocks[i];
          const titleMatch = b.match(/class="result__a"[^>]*>([^<]+)</);
          const urlMatch = b.match(/class="result__url"[^>]*>([^<]+)</);
          const snippetMatch = b.match(/class="result__snippet"[^>]*>([^<]+)</);
          if (titleMatch && snippetMatch) {
            results.push({
              title: titleMatch[1].replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">"),
              url: urlMatch ? urlMatch[1].trim() : "",
              snippet: snippetMatch[1].replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">"),
            });
          }
        }
        
        if (results.length === 0) {
          return {
            content: [{ type: "text", text: "No results found." }],
            details: { query, count: 0 },
          };
        }
        
        const text = results.map((r, i) =>
          `${i + 1}. ${r.title}\n   ${r.url}\n   ${r.snippet}`
        ).join("\n\n");
        
        return {
          content: [{ type: "text", text }],
          details: { query, count: results.length },
        };
      } catch (e: any) {
        throw new Error(`web_search failed: ${e?.message ?? e}`);
      }
    },
  });

  // ── web_scrape ──
  pi.registerTool({
    name: "web_scrape",
    label: "Web Scrape",
    description:
      "Fetch and extract text content from a URL. Returns clean text (no HTML). " +
      "Use after web_search to read a page.",
    promptSnippet: "Fetch and extract text content from a URL",
    parameters: Type.Object({
      url: Type.String({ description: "Full URL to fetch" }),
    }),

    async execute(_toolCallId, params, signal, onUpdate) {
      const url = params.url as string;

      onUpdate?.({
        content: [{ type: "text", text: `Fetching: ${url}` }],
      });

      if (signal?.aborted) {
        return { content: [{ type: "text", text: "Cancelled" }], details: {} };
      }

      try {
        const resp = await fetch(url, {
          headers: { "User-Agent": "Mneme/1.0" },
          signal: AbortSignal.timeout(15000),
        });
        const contentType = resp.headers.get("content-type") || "";
        
        let text: string;
        // Handle plain text / markdown directly
        if (contentType.includes("text/plain") || url.endsWith(".md") || url.endsWith(".txt")) {
          text = (await resp.text()).slice(0, 15000);
        } else {
          // HTML — strip tags, extract body text
          const html = await resp.text();
          text = html
            .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, "")
            .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, "")
            .replace(/<[^>]+>/g, " ")
            .replace(/&amp;/g, "&")
            .replace(/&lt;/g, "<")
            .replace(/&gt;/g, ">")
            .replace(/&quot;/g, '"')
            .replace(/&#39;/g, "'")
            .replace(/&nbsp;/g, " ")
            .replace(/\s+/g, " ")
            .trim()
            .slice(0, 15000);
        }
        
        return {
          content: [{ type: "text", text: text || "(no text content found)" }],
          details: { url, charCount: text.length },
        };
      } catch (e: any) {
        throw new Error(`web_scrape failed: ${e?.message ?? e}`);
      }
    },
  });
}
