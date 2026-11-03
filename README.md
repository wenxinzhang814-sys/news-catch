# News Catch

[English](README.md) | [简体中文](README.zh-CN.md)

A local, browser-based workspace for collecting news, filtering articles, analyzing them with AI, reviewing results, and delivering structured data through Webhooks. Its configurable classification engine supports different industries and research topics. Built with a Python backend, plain HTML/CSS/JavaScript, and SQLite.

The interface and generated summaries currently use Chinese.

## Clean starting state

No news sources, keyword rules, target regions, model endpoint/name/key, Webhook, or field mappings are preconfigured. The repository contains no original user's articles, source text, delivery history, database, or internal task documents. Configure these settings on first use.

The default taxonomy covers products and services, technology and innovation, markets and business, policy and regulation, organizations and partnerships, risks and events, and other news. Customize category names, definitions, keywords, suggested actions, and region names/aliases in the browser. There are no automotive-specific topic restrictions, company lists, or fixed five-category mappings.

## Features

- Manage RSS feeds and web sources, import OPML files, test sources, and monitor source health.
- Extract webpage text, supplement it manually, filter with Boolean keyword expressions, deduplicate articles, and identify regions and categories.
- Use a configurable OpenAI-compatible model API for individual or batch analysis, background jobs, long-text segmentation, evidence validation, and Chinese summaries.
- Set your industry context, classification taxonomy, suggested actions, priority/exclusion keywords, and regions; review and Webhook output use the configured values.
- Edit and approve articles, manage the delivery queue, return items for review, and soft-delete or restore records.
- Add, remove, and map Webhook fields; edit the JSON preview; retry failed deliveries; and inspect delivery history.
- Track webpage snapshot changes, view run status, schedule tasks, and generate Markdown weekly reports.

## Start on Windows

1. Install **Python 3.11 or newer**, with Python Launcher enabled or Python added to `PATH`.
2. Download and fully extract the repository, then double-click **start.cmd**. On first launch, it creates `.venv` and installs the pinned dependencies over the network. Later launches reuse that environment.
3. The browser opens automatically. In **分析与分类** (Analysis & Classification), set your industry/topic, business context, categories, and suggested actions. Save the profile; you can return to change it at any time.
4. In **来源与规则** (Sources & Rules), add sources or import OPML, add named rules, enter their expressions, and save. Expressions support `AND`, `OR`, `NOT`, parentheses, and double-quoted phrases. Ordinary news articles do not enter the pool when no rules are configured.
5. In **模型与 Webhook** (Model & Webhook), enter your model endpoint, model name, API key, and Webhook URL. Use **新增字段** (Add field) to configure mappings for the receiver. Deliveries containing articles are blocked until at least one field mapping is configured.
6. Click **立即运行** (Run now), then fetch article text, analyze, review, preview, and deliver as needed.

The first installation needs access to a Python package index. Collection and model calls also require access to the relevant websites and APIs. This source edition does not bundle Python or accounts. If you upgrade the system Python installation, remove this project's `.venv` and launch again to rebuild it.

## Configure a classification profile

For a healthcare workspace, set the industry to healthcare and describe which developments matter to your team. Define categories such as clinical research, products and devices, and regulation, with a short decision rule and relevant keywords for each. A retail workspace can use a different taxonomy and suggested actions without changing source code.

The **分析与分类** page controls the analysis profile. Suggested actions, priority keywords, and exclusion keywords use one entry per line. Region definitions use `Name | alias, alias`, one region per line. The selected target regions in **来源与规则** remain a separate collection filter; select none to leave the geographic scope unrestricted. Boolean rules determine which ordinary news articles enter the pool, while priority keywords affect candidate sorting. Exclusion keywords remove unwanted matches.

AI analysis uses the saved industry context and definitions, with source quotations required as evidence. It must select configured categories and actions. Without a usable model result, unmatched articles remain **未分类** (Unclassified) and pending review rather than receiving an invented classification. Saving a profile changes subsequent collection and analysis; it does not rewrite existing articles or delivery history. Existing category/action values remain visible when reviewing older records.

Webhook `category` and `company` values preserve the analysis or human-edited content instead of converting them to a fixed industry-specific list. The `language` field accepts arbitrary language names and outputs multiple values as an array. Region output remains a single option string, so configure compatible options in your receiving table. Adjust field mappings and receiver conversions for your own schema.

For compatibility, the five stored stage values remain `仅宣布` (announced/planned), `测试中` (trial/pilot), `灰度` (phased implementation), `已上线` (implemented/effective), and `待确认` (unconfirmed). Specific business milestones belong in the change description. Changes to a profile during analysis prevent the old result from overwriting saved content. See [CHANGELOG.md](CHANGELOG.md) for release details.

## Manual start / Linux / macOS

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -B launch.py
```

On Windows, use `.venv\Scripts\python.exe` instead. The main application logic is cross-platform, but this distribution has only been verified on Windows x64 with Python 3.11. Linux and macOS have not been tested.

The launcher listens only on `127.0.0.1` and selects an available port from 4173–4192. Optional arguments include `--port 4216`, `--no-browser`, and `--no-scheduler`. Closing the terminal stops the service and scheduled tasks; tasks do not run while the computer is asleep. Scheduling uses Asia/Shanghai (GMT+8), with a default time of 10:00. Starting after the scheduled time may trigger a catch-up run.

## Checks and tests

After installing dependencies, use the virtual environment on Windows:

```powershell
.\.venv\Scripts\python.exe -B launch.py --check
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

On Linux or macOS, use `.venv/bin/python` instead.

`--check` uses temporary data to validate dependencies and the empty default configuration without calling a model or Webhook. Tests use isolated directories and mocked responses. Use `--data-dir` to select a separate data directory.

## Data and sharing

After startup, `workbench/data/config.json` stores personal settings, including the API key and Webhook URL. `workbench/data/trenddesk.sqlite3` stores articles and history. Keep this directory private and back up the entire `workbench/data` directory after stopping the service.

This is a local, single-computer workspace. Data does not automatically sync between computers.

## Publishing or sharing your copy

Use this folder as the repository root. `.gitignore` excludes runtime data, databases, secret files, virtual environments, and test output. The initial source package excludes private configuration; review staged files before publishing a copy that you have already used.

## Project structure

```text
News Catch/
  start.cmd / bootstrap.py  Windows dependency setup and launch
  launch.py                Local paths, isolated data, and port selection
  workbench/               Application source and pinned dependencies
  tests/                   Clean-configuration regression tests
  LICENSE                  MIT
```

## Current limitations

Web extraction cannot guarantee access to pages requiring login, pages protected against automated access, or content rendered only by client-side scripts. Paste the source text manually when needed. AI results require human review.

There are no multi-user account permissions or hardening for public internet deployment. The application is intended for local use; cloud collaboration requires further changes. Successful writes through Webhooks depend on the receiver's response and compatible field types.

## License

Project code is licensed under the [MIT License](LICENSE). Third-party dependencies retain their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
