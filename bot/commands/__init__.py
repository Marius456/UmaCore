"""
Bot commands package
"""

async def setup(bot):
    """Load all command cogs"""
    from .settings import SettingsCommands
    from .admin import AdminCommands
    from .member import MemberCommands
    from .club_management import ClubManagementCommands
    from .author import AuthorCommands
    from .charts import ChartCommands
    from .leaderboard import LeaderboardCommands
    from .gacha import GachaCommands
    from .trivia import TriviaCommands
    from .help import HelpCommands

    await bot.add_cog(SettingsCommands(bot))
    await bot.add_cog(AdminCommands(bot))
    await bot.add_cog(MemberCommands(bot))
    await bot.add_cog(ClubManagementCommands(bot))
    await bot.add_cog(AuthorCommands(bot))
    await bot.add_cog(ChartCommands(bot))
    await bot.add_cog(LeaderboardCommands(bot))
    await bot.add_cog(GachaCommands(bot))
    await bot.add_cog(TriviaCommands(bot))
    await bot.add_cog(HelpCommands(bot))
