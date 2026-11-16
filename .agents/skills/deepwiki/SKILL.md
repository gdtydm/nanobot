---
name: deepwiki
description: Navigate or query DeepWiki's repository documentation to orient a source-code investigation and locate relevant code. Use when the user requests DeepWiki or repository-level background.
---

# DeepWiki repository research

DeepWiki is an optional external knowledge source, not a substitute for the current checkout. Identify the repository from the user's request or its Git remote; verify the owner and repository before querying. For public GitHub repositories, the wiki is available at `https://deepwiki.com/<owner>/<repo>`. For example, the configured upstream may be viewed at https://deepwiki.com/HKUDS/nanobot; check the current remote before assuming it matches the code under study.

If a DeepWiki MCP connection is already available, use its repository structure, wiki contents and question tools as appropriate. The official public endpoint is `https://mcp.deepwiki.com/mcp`; tool names and availability depend on the connected client. Otherwise use the web page if accessible. Do not change global or project connection settings automatically. If the service, repository or access is unavailable, explain the limitation and proceed with local sources.

Ask focused questions to discover architecture, entry points or candidate paths. Treat generated answers and links as leads: inspect the actual files and revision in the current checkout, verify material claims and report discrepancies or unknowns. Cite the DeepWiki page for its claims and local file paths and lines for conclusions about current behavior. Do not send private code, credentials or sensitive details to an external service without authorization.
