# ChatGPT integration boundary

The server uses the official MCP protocol and can expose Streamable HTTP for local development. ChatGPT's hosted connector needs a reachable authenticated service and a file-transfer path. Those production components are not implemented in this release; do not publicly expose the unauthenticated loopback server.

The provider-neutral tools and `skills/` instructions can be packaged after authentication, tenant separation, uploads/downloads and host compatibility are implemented. Follow the official [MCP server integration guide](https://developers.openai.com/plugins/build/app-quickstart) at that time. No OpenAI API key is needed for local media processing, and this repository does not call OpenAI APIs.
