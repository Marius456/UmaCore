# FAQ

## How do I set up the bot?

Use `/add_club` to register your club, then `/set_report_channel` to set where reports go. After that either wait for the automatic daily scrape or run `/force_check` to trigger it immediately.

See the [Getting Started](getting-started.md) guide for the full walkthrough.

---

## How do I find my circle ID?

**Uma.moe:** Go to [uma.moe/circles](https://uma.moe/circles/), search for your club, and copy the number from the URL. It looks something like `860280110`.

---

## When does fan data update?

Uma.moe generally publishes a new daily snapshot around 15:10 UTC. Configure a
club-local scrape time comfortably after that instant.

---

## The bot didn't scrape at the time I set

The scheduler checks once per hour. It runs on the first check at or after the
configured club-local time, so a report may be up to roughly one hour late.

---

## How do I invite the bot to my server?

You can invite the public bot using the link below, or self-host it since the project is open source.

[Invite UmaCore](https://discord.com/oauth2/authorize?client_id=1467295225184784488&permissions=83968&integration_type=0&scope=bot+applications.commands)

---

## I have a problem

Please describe your issue in detail in the **#support** channel on our Discord. Keep in mind that responses may not be instant.
