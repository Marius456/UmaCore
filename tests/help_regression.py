import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from discord.ext import commands

from bot.commands import setup
from bot.commands.help import HelpCommands


class HelpCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_guide_is_private_and_within_embed_limits(self):
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        cog = HelpCommands(None)
        await cog.help.callback(cog, interaction)

        interaction.response.send_message.assert_awaited_once()
        response = interaction.response.send_message.call_args.kwargs
        self.assertTrue(response["ephemeral"])
        embed = response["embed"]
        self.assertEqual(embed.title, "UmaCore Help")
        self.assertLessEqual(len(embed), 6000)
        self.assertLessEqual(len(embed.description), 4096)
        self.assertEqual(
            [field.name for field in embed.fields],
            ["Your account", "Clubs and progress", "Gacha and trivia", "Information"],
        )
        for field in embed.fields:
            self.assertLessEqual(len(field.name), 256)
            self.assertLessEqual(len(field.value), 1024)
            self.assertFalse(field.inline)

    async def test_extension_registers_help_and_guide_matches_member_commands(self):
        # The existing trivia cog seeds its database on load; keep registration offline.
        async with commands.Bot(command_prefix="!", intents=discord.Intents.none()) as bot:
            with patch("bot.commands.trivia.TriviaCommands.cog_load", new=AsyncMock()):
                await setup(bot)
            command = bot.tree.get_command("help")
            self.assertIsNotNone(command)
            self.assertEqual(command.parameters, [])
            self.assertEqual(command.checks, [])
            self.assertIsNone(command.default_permissions)

            interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
            await command.callback(command.binding, interaction)
            embed = interaction.response.send_message.call_args.kwargs["embed"]
            entries = re.findall(r"`/([^`]+)`", "\n".join(f.value for f in embed.fields))
            actual = {}
            for entry in entries:
                name = entry.split(" <")[0]
                actual[name] = re.findall(r"<([^>]+)>", entry)
            self.assertEqual(set(actual), {
                "link_trainer", "unlink", "my_status", "notification_settings",
                "list_clubs", "member_status", "progress_chart", "previous_month",
                "gacha", "trivia play", "trivia leaderboard", "privacy",
            })
            registered = {cmd.qualified_name: cmd for cmd in bot.tree.walk_commands()}
            for name, arguments in actual.items():
                with self.subTest(command=name):
                    member_command = registered[name]
                    self.assertEqual(member_command.checks, [])
                    self.assertEqual(
                        arguments, [p.name for p in member_command.parameters if p.required]
                    )