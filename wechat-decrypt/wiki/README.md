# WeChat Reply Wiki Architecture

## First principle

`wiki/core/` is always loaded for every monitored chat. It contains identity, allowed actions, forbidden actions, and reply boundaries. These rules are the top-level design and must not be bypassed by any scene knowledge base, online provider, or target configuration.

Do not store secrets, tokens, database paths, private chat logs, or internal automation details in wiki files.

## Knowledge base model

Below `core`, knowledge is organized by reusable scenes rather than by WeChat group name. A monitored group can choose 0, 1, or many knowledge bases according to its attributes.

Recommended local layout:

```text
wiki/
  core/                       # mandatory first principles, always loaded
  scenes/
    bot_testing/              # reusable scene KB
    chongqing_mobile_work/    # reusable scene KB
  providers/
    ima/                      # IMA integration notes/config examples only
```

Config example:

```json
{
  "knowledge_bases": {
    "scene.bot_testing": {
      "type": "local",
      "path": "scenes/bot_testing",
      "scope": "scene"
    },
    "online.ima.company_faq": {
      "type": "ima",
      "agent_id": "...",
      "api_key_env": "IMA_API_KEY"
    }
  },
  "targets": [
    {
      "name": "some group",
      "knowledge_bases": ["scene.bot_testing", "online.ima.company_faq"]
    }
  ]
}
```

A target with `knowledge_bases: []` uses only `wiki/core` and therefore only replies within the global boundaries.

## Legacy cleanup

Older physical folders `wiki/local/`, `wiki/groups/`, and group-name folders are deprecated. They should be migrated into `wiki/scenes/<scene_id>/` and selected from target config.
