# UmaCore

UmaCore is a Discord bot for tracking and managing Umamusume club member quotas. It handles daily fan-earning goals, deficit notifications, progress charts, and member management — all automatically.

## Features

- Multi-club support with independent settings per club
- Automated daily quota checks at configurable times
- Consecutive days-behind tracking for members falling behind
- Progress charts showing fan progression throughout the month
- Member auto-detection (adds new members, deactivates those who leave)
- Month-bounded quota history and streak handling
- Discord account linking for personal DM notifications
- Uma.moe API ingestion with a Playwright transport fallback

## Navigation

- [Getting Started](getting-started.md) — Invite the bot and get up and running
- [FAQ](faq.md) — Common questions and issues
- [Setup Guide](setup.md) — Self-hosting and configuration
- [Commands](commands/README.md) — All slash commands
- [Quota System](quota-system.md) — How quotas and deficit tracking work
- [Data Source](data-sources.md) — Uma.moe API behavior and failure handling
