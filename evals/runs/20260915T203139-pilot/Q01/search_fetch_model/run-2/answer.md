**Direct answer:** The officially documented ways to add Ollama web search are: direct REST calls, Ollama’s Python library, Ollama’s JavaScript library, using the same functions as tools in an Ollama chat/tool-calling agent, and MCP-client integration via Ollama’s Python MCP server. The fully specified return shapes are for `web_search` and `web_fetch`; MCP/OpenAI-compatible integration returns are not fully specified in the provided official docs.

| Official interface | What you call | What it returns |
|---|---|---|
| **REST Web Search API** | `POST https://ollama.com/api/web_search` with `query` and optional `max_results` | An object with `results`, an array of search result objects. Each result has `title`, `url`, and `content` snippet fields [1]. |
| **Python library: web search** | `ollama.web_search("What is Ollama?")` | The examples show a response containing `results`, where each result has `title`, `url`, and `content` [1][2]. The docs do not name the Python response class for `web_search`; an agent example displays tool output as `results=[WebSearchResult(...)]` [1]. |
| **JavaScript library: web search** | `client.webSearch(...)` | A JSON-serializable object with `results`, an array of objects containing `title`, `url`, and `content` [1][2]. |
| **REST Web Fetch API** | `POST https://ollama.com/api/web_fetch` with `url` | An object with `title`, `content`, and `links` fields; `links` is an array of links found on the page [1]. This is the official companion interface for fetching individual pages after search or when the user provides a URL [2]. |
| **Python library: web fetch** | `web_fetch("https://ollama.com")` | A `WebFetchResponse` with `title`, `content`, and `links` [1][2]. |
| **JavaScript library: web fetch** | `client.webFetch(...)` | A JSON-serializable object with `title`, `content`, and `links` [1][2]. |
| **Ollama chat/tool-calling agent** | `chat(..., tools=[web_search, web_fetch])` in the official Python example | The chat response can include `message.thinking`, `message.content`, and `message.tool_calls`; a tool call can request `web_search` with arguments such as `query` and `max_results`, and the invoked tool returns the same kind of `web_search`/`web_fetch` result described above [1]. |
| **MCP server integration** | Configure Ollama’s Python MCP server in an MCP client such as Cline, Codex, or Goose | **Incomplete evidence:** Ollama’s docs say web search can be enabled in any MCP client through the Python MCP server and give setup examples, but they do not specify a distinct MCP return schema [1]. |

**Sourced facts:** Ollama says web search is provided as a REST API with deeper integrations in the Python and JavaScript libraries [1][2]. Access to the hosted web search API requires an Ollama API key, and the docs say a free Ollama account is required [1].

**Interpretation:** I am treating “official” as interfaces documented by Ollama itself on `docs.ollama.com` or `ollama.com/blog`. The `web_fetch` interfaces are not search-result interfaces, but they are officially documented on the same Web Search capability page and are used in Ollama’s official search-agent example, so they are part of the official web-search workflow [1][2].

**Conflicting or incomplete evidence:** The official docs and blog show different JavaScript call shapes: the docs show `client.webSearch("what is ollama?")` and `client.webFetch("https://ollama.com")`, while the blog shows `client.webSearch({ query: "what is ollama?" })` and `client.webFetch({ url: "https://ollama.com" })`; both sources show the same return shapes [1][2]. MCP integration is officially mentioned, but its return schema is not documented in the provided official material [1].

## Sources

[1] Ollama Docs — Web search: https://docs.ollama.com/capabilities/web-search  
[2] Ollama Blog — Web search: https://ollama.com/blog/web-search
