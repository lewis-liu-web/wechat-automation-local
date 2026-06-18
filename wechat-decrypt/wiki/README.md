# WeChat Reply Wiki Architecture

## First principle

`wiki/core/` is always loaded for every monitored chat. It contains identity, allowed actions, forbidden actions, and reply boundaries. These rules are the top-level design and must not be bypassed by any scene knowledge base, online provider, or target configuration.

Do not store secrets, tokens, database paths, private chat logs, or internal automation details in wiki files.

## Knowledge base model

Below `core`, knowledge is organized by reusable scenes rather than by WeChat group name. A monitored group can choose **exactly one** knowledge-base source (local or online) according to its attributes.

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
      "knowledge_base_id": "YOUR_KB_ID",
      "api_key_env": "IMA_API_KEY"
    }
  },
  "targets": [
    {
      "name": "some group",
      "knowledge_bases": ["scene.bot_testing"]
    }
  ]
}
```

A target with `knowledge_bases: []` uses only `wiki/core` and therefore only replies within the global boundaries.

## Local KB indexing

Local KBs are indexed with SQLite FTS5 using the `trigram` tokenizer. This makes Chinese (CJK) body content searchable without extra dependencies.

- Each local KB directory gets a `.kb_index.sqlite` file.
- The index is rebuilt automatically when the schema version changes.
- To force a rebuild, run `python manage_targets.py kb-reindex <alias>`.
- To inspect index state and test a query, run `python manage_targets.py kb-diagnose <alias> --query "关键词"`.
- Raw chat messages are cleaned (sender prefix, @mentions, filler words) before FTS search, so keep product keywords in the document body.

## Global toggle

Set `reply_engine.disable_local_kb: true` to skip all local KBs and rely only on online providers or `wiki/core` for every target.

## Single-source binding rule

Each target may be bound to at most one knowledge-base alias. Binding two aliases, or mixing local with online sources, is rejected by the control API and CLI.

## Legacy cleanup

Older physical folders `wiki/local/`, `wiki/groups/`, and group-name folders are deprecated. They should be migrated into `wiki/scenes/<scene_id>/` and selected from target config.
