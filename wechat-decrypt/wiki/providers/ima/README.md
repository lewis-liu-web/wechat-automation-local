# IMA Provider Notes

IMA is supported as an optional online knowledge source for WeChat automatic replies.

Verified source:

- Skill package: https://app-dl.ima.qq.com/skills/ima-skills-1.1.7.zip
- Verified package: `ima-skills-1.1.7.zip`, sha256 `bd5878416aed4c358deb2b9bc34dfd7602a0da9d9602582227794791289f73b2`
- API Key page: https://ima.qq.com/agent-interface
- Official endpoint: `POST https://ima.qq.com/openapi/wiki/v1/search_knowledge`
- Required headers: `ima-openapi-clientid`, `ima-openapi-apikey`, `ima-openapi-ctx`, `Content-Type: application/json`
- Request body: `{ "query": "...", "cursor": "", "knowledge_base_id": "..." }`

Security rule: do not commit API keys. Configure credentials only through environment variables named by the KB spec, for example:

```json
{
  "knowledge_bases": {
    "online.ima.project": {
      "type": "ima",
      "knowledge_base_id": "YOUR_IMA_KB_ID",
      "client_id_env": "IMA_CLIENT_ID",
      "api_key_env": "IMA_API_KEY",
      "limit": 3
    }
  }
}
```

If credentials or `knowledge_base_id` are missing, or if the network/API call fails, the adapter returns no online hits and the reply engine still uses `wiki/core` first-principle boundaries.

