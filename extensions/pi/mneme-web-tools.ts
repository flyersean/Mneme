// Mneme web tools extension for Pi
// Adds web_search and web_scrape — no API keys needed.
// Uses DuckDuckGo for search, plain HTTP fetch for scraping.
//
// Usage: pi --extension /workspace/mneme-web-tools.ts

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function setup(api: ExtensionAPI) {
  
  // ── web_search ──
  api.registerTool({
    name: "web_search",
    description: "Search the web using DuckDuckGo. Returns titles, URLs, and snippets. Free, no API key required.",
    parameters: {
      type: "object",
      properties: {
        query: { type: "string", description: "What to search for" },
        limit: { type: "number", description: "Max results (default 5)", default: 5 },
      },
      required: ["query"],
    },
    execute: async ({ query, limit = 5 }) => {
      try {
        const url = `https://html.duckduckgo.com/html/?q=${encodeURIComponent(query)}`;
        const resp = await fetch(url, {
          headers: { "User-Agent": "Mneme/1.0" },
          signal: AbortSignal.timeout(10000),
        });
        const html = await resp.text();
        
        // Extract result blocks
        const results: { title: string; url: string; snippet: string }[] = [];
        const blocks = html.split('class="result"');
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
        
        if (results.length === 0) return "No results found.";
        return results.map((r, i) => 
          `${i + 1}. ${r.title}\n   ${r.url}\n   ${r.snippet}`
        ).join("\n\n");
      } catch (e: any) {
        return `Search failed: ${e.message}`;
      }
    },
  });

  // ── web_scrape ──
  api.registerTool({
    name: "web_scrape",
    description: "Fetch and extract text content from a URL. Returns clean text (no HTML). Works with most websites. Use after web_search to read a page.",
    parameters: {
      type: "object",
      properties: {
        url: { type: "string", description: "Full URL to fetch" },
      },
      required: ["url"],
    },
    execute: async ({ url }) => {
      try {
        const resp = await fetch(url, {
          headers: { "User-Agent": "Mneme/1.0" },
          signal: AbortSignal.timeout(15000),
        });
        const contentType = resp.headers.get("content-type") || "";
        
        // Handle plain text / markdown directly
        if (contentType.includes("text/plain") || url.endsWith(".md") || url.endsWith(".txt")) {
          return (await resp.text()).slice(0, 15000);
        }
        
        // HTML — strip tags, extract body text
        const html = await resp.text();
        let text = html
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
          .trim();
        
        return text.slice(0, 15000) || "(no text content found)";
      } catch (e: any) {
        return `Scrape failed: ${e.message}`;
      }
    },
  });

}
