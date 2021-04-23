import discord
import os
import asyncio
import time
import functools
import itertools
import math
import random
import youtube_dl
from async_timeout import timeout
import discord.ext
from discord.utils import get
from discord.ext import commands, tasks
from discord.ext.commands import has_permissions,  CheckFailure, check
import logging

#this is for logging
logger = logging.getLogger('discord')
logger.setLevel(logging.DEBUG)
handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
logger.addHandler(handler)

#imports the keep_alive function
from alive import keep_alive

#tells the program that client is the discord client
client = discord.Client()

#gives the commands a prefix
client = commands.Bot(command_prefix = '/', case_insensitive=True)

# Silence useless bug reports messages
youtube_dl.utils.bug_reports_message = lambda: ''


class VoiceError(Exception):
    pass


class YTDLError(Exception):
    pass


class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
    }

    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn',
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, ctx: commands.Context, source: discord.FFmpegPCMAudio, *, data: dict, volume: float = 0.5):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        date = data.get('upload_date')
        self.upload_date = date[6:8] + '.' + date[4:6] + '.' + date[0:4]
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.stream_url = data.get('url')

    def __str__(self):
        return '**{0.title}** by **{0.uploader}**'.format(self)

    @classmethod
    async def create_source(cls, ctx: commands.Context, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(cls.ytdl.extract_info, search, download=False, process=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))

        if 'entries' not in data:
            process_info = data
        else:
            process_info = None
            for entry in data['entries']:
                if entry:
                    process_info = entry
                    break

            if process_info is None:
                raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))

        webpage_url = process_info['webpage_url']
        partial = functools.partial(cls.ytdl.extract_info, webpage_url, download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise YTDLError('Couldn\'t fetch `{}`'.format(webpage_url))

        if 'entries' not in processed_info:
            info = processed_info
        else:
            info = None
            while info is None:
                try:
                    info = processed_info['entries'].pop(0)
                except IndexError:
                    raise YTDLError('Couldn\'t retrieve any matches for `{}`'.format(webpage_url))

        return cls(ctx, discord.FFmpegPCMAudio(info['url'], **cls.FFMPEG_OPTIONS), data=info)

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration = []
        if days > 0:
            duration.append('{} days'.format(days))
        if hours > 0:
            duration.append('{} hours'.format(hours))
        if minutes > 0:
            duration.append('{} minutes'.format(minutes))
        if seconds > 0:
            duration.append('{} seconds'.format(seconds))

        return ', '.join(duration)


class Song:
    __slots__ = ('source', 'requester')

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (discord.Embed(title='Now playing',
                               description='```css\n{0.source.title}\n```'.format(self),
                               color=discord.Color.blurple())
                 .add_field(name='Duration', value=self.source.duration)
                 .add_field(name='Requested by', value=self.requester.mention)
                 .add_field(name='Uploader', value='[{0.source.uploader}]({0.source.uploader_url})'.format(self))
                 .add_field(name='URL', value='[Click]({0.source.url})'.format(self))
                 .set_thumbnail(url=self.source.thumbnail))

        return embed


class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx

        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()

        self._loop = False
        self._volume = 0.5
        self.skip_votes = set()

        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear()

            if not self.loop:
                # Try to get the next song within 3 minutes.
                # If no song will be added to the queue in time,
                # the player will disconnect due to performance
                # reasons.
                try:
                    async with timeout(180):  # 3 minutes
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    self.bot.loop.create_task(self.stop())
                    return

            self.current.source.volume = self._volume
            self.voice.play(self.current.source, after=self.play_next_song)
            await self.current.source.channel.send(embed=self.current.create_embed())

            await self.next.wait()

    def play_next_song(self, error=None):
        if error:
            raise VoiceError(str(error))

        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            self.voice = None


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states = {}

    def get_voice_state(self, ctx: commands.Context):
        state = self.voice_states.get(ctx.guild.id)
        if not state:
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state

        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    def cog_check(self, ctx: commands.Context):
        if not ctx.guild:
            embed = discord.Embed(description= '🔔  This command can\'t be used in DM channels 🔔', color=discord.Color.red())
            raise commands.NoPrivateMessage(embed=embed)

        return True

    async def cog_before_invoke(self, ctx: commands.Context):
        ctx.voice_state = self.get_voice_state(ctx)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        await ctx.send('An error occurred: {}'.format(str(error)))

    @commands.command(name='join', invoke_without_subcommand=True)
    async def _join(self, ctx: commands.Context):
        """Joins a voice channel."""

        destination = ctx.author.voice.channel

        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()
        embed = discord.Embed(description="🔔  I'm here! It's time to vibe! 🔔", color=discord.Color.green())
        await ctx.send(embed=embed, delete_after=30)
        await ctx.message.delete()

    @commands.command(name='summon')
    @commands.has_permissions(manage_guild=True)
    async def _summon(self, ctx: commands.Context, *, channel: discord.VoiceChannel = None):
        """Summons the bot to a voice channel.

        If no channel was specified, it joins your channel.
        """

        if not channel and not ctx.author.voice:
            embed = discord.Embed(description= "🔔  You are neither connected to a voice channel nor specified a channel to join 🔔", color=discord.Color.red())
            raise VoiceError(embed=embed, delete_after=5)

        destination = channel or ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name='leave', aliases=['disconnect'])
    @commands.has_permissions(manage_guild=True)
    async def _leave(self, ctx: commands.Context):
        """Clears the queue and leaves the voice channel."""

        if not ctx.voice_state.voice:
            embed = discord.Embed(description= "🔔  I am not currently connected to any voice channels 🔔", color=discord.Color.red())
            return await ctx.send(embed=embed, delete_after=5)

        await ctx.voice_state.stop()
        del self.voice_states[ctx.guild.id]
        embed = discord.Embed(description = "🔔  I'll see you next time! 🔔", color=discord.Color.gold())
        await ctx.send(embed=embed, delete_after=10)


    @commands.command(name='volume')
    async def _volume(self, ctx: commands.Context, *, volume: int):
        """Sets the volume of the player."""

        if not ctx.voice_state.is_playing:
            embed = discord.Embed(description= "🔔  Nothing is playing at the moment 🔔", color=discord.Color.red())
            return await ctx.send(embed=embed, delete_after=5)

        if 0 > volume > 100:
            embed = discord.Embed(description= "🔔  Volume must be between 0 and 100 🔔", color=discord.Color.red())
            return await ctx.send(embed=embed, delete_after=5)

        ctx.voice_state.volume = volume / 100
        await ctx.send('Volume of the player set to {}%'.format(volume))

    @commands.command(name='now', aliases=['current', 'playing'])
    async def _now(self, ctx: commands.Context):
        """Displays the currently playing song."""

        await ctx.send(embed=ctx.voice_state.current.create_embed())

    @commands.command(name='pause')
    @commands.has_permissions(manage_guild=True)
    async def _pause(self, ctx: commands.Context):
        """Pauses the currently playing song."""

        if ctx.voice_state.is_playing and ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='resume')
    @commands.has_permissions(manage_guild=True)
    async def _resume(self, ctx: commands.Context):
        """Resumes a currently paused song."""

        if ctx.voice_state.is_playing and ctx.voice_state.voice.is_paused():
            ctx.voice_state.voice.resume()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='stop')
    @commands.has_permissions(manage_guild=True)
    async def _stop(self, ctx: commands.Context):
        """Stops playing song and clears the queue."""

        ctx.voice_state.songs.clear()

        if ctx.voice_state.is_playing:
            ctx.voice_state.voice.stop()
            await ctx.message.add_reaction('⏹')

    @commands.command(name='skip')
    async def _skip(self, ctx: commands.Context):
        """Vote to skip a song. The requester can automatically skip.
        3 skip votes are needed for the song to be skipped.
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send('Not playing any music right now...')

        voter = ctx.message.author
        if voter == ctx.voice_state.current.requester:
            await ctx.message.add_reaction('⏭')
            ctx.voice_state.skip()

        elif voter.id not in ctx.voice_state.skip_votes:
            ctx.voice_state.skip_votes.add(voter.id)
            total_votes = len(ctx.voice_state.skip_votes)

            if total_votes >= 3:
                await ctx.message.add_reaction('⏭')
                ctx.voice_state.skip()
            else:
                await ctx.send('Skip vote added, currently at **{}/3**'.format(total_votes))

        else:
            await ctx.send('You have already voted to skip this song.')

    @commands.command(name='queue')
    async def _queue(self, ctx: commands.Context, *, page: int = 1):
        """Shows the player's queue.

        You can optionally specify the page to show. Each page contains 10 elements.
        """

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ''
        for i, song in enumerate(ctx.voice_state.songs[start:end], start=start):
            queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(i + 1, song)

        embed = (discord.Embed(description='**{} tracks:**\n\n{}'.format(len(ctx.voice_state.songs), queue))
                 .set_footer(text='Viewing page {}/{}'.format(page, pages)))
        await ctx.send(embed=embed)

    @commands.command(name='shuffle')
    async def _shuffle(self, ctx: commands.Context):
        """Shuffles the queue."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        ctx.voice_state.songs.shuffle()
        await ctx.message.add_reaction('✅')

    @commands.command(name='remove')
    async def _remove(self, ctx: commands.Context, index: int):
        """Removes a song from the queue at a given index."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        ctx.voice_state.songs.remove(index - 1)
        await ctx.message.add_reaction('✅')

    @commands.command(name='loop')
    async def _loop(self, ctx: commands.Context):
        """Loops the currently playing song.

        Invoke this command again to unloop the song.
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send('Nothing being played at the moment.')

        # Inverse boolean value to loop and unloop.
        ctx.voice_state.loop = not ctx.voice_state.loop
        await ctx.message.add_reaction('✅')

    @commands.command(name='play')
    async def _play(self, ctx: commands.Context, *, search: str):
        """Plays a song.

        If there are songs in the queue, this will be queued until the
        other songs finished playing.

        This command automatically searches from various sites if no URL is provided.
        A list of these sites can be found here: https://rg3.github.io/youtube-dl/supportedsites.html
        """

        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        async with ctx.typing():
            try:
                source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
            except YTDLError as e:
                embed= discord.Embed(description= '🔔  An error occurred while processing this request: {} 🔔'.format(str(e)), color=discord.Color.red())
                await ctx.send(embed=embed, delete_after=5)
            else:
                song = Song(source)

                await ctx.voice_state.songs.put(song)
                embed = discord.Embed(description= '🔔  Enqueued ' + '{} 🔔'.format(str(source)), color= discord.Color.gold())
                await ctx.send(embed=embed)

    @_join.before_invoke
    @_play.before_invoke
    async def ensure_voice_state(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            embed = discord.Embed(description="🔔  You are not connected to any voice channel 🔔", color=discord.Color.red())
            raise commands.CommandError(embed=embed, delete_after=5)

        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                embed = discord.Embed(description="🔔  I am already in a voice channel. 🔔", color=discord.Color.gold())
                raise commands.CommandError(embed=embed, delete_after=5)

#tells the program that client is the discord client

client.add_cog(Music(client))

#This is what tells you that the bot is on
@client.event
async def on_ready():
    print('🔔  {0.user}'.format(client) + ' Has Awoken! 🔔')

#the pray command
@client.command()
async def pray(ctx):
    embed = discord.Embed(description='🔔 The Holy Bell has blessed you! 🔔', color=discord.Color.gold())
    await ctx.send(embed=embed)
    print("🔔  " + ctx.author.name + " has prayed to the Holy Bell 🔔")

#this is the list command which is pretty much just help but discord is mean
@client.command()
async def list(ctx):
    embed = discord.Embed(description= '```css\nList of commands:\n$pray\n  You pray to the Holy Bell in hopes that you get blessed\n$bellbless\n  The almighty Holy Bell will bless any bells that you hold```', color=discord.Color.green())
    await ctx.send(embed=embed)

#This is the bellbless command when you wanna get a bell blessed
@client.command()
async def bellbless(ctx):
    embed = discord.Embed(description= "I bless this bell!", color=discord.Color.gold())
    await ctx.send(embed=embed, delete_after=5)
    print(f"{ctx.author.name} has had a bell blessed!")

#THIS IS FOR WHEN I FEEL LIKE DECLARING WAR ON PEOPLE
@client.command(pass_context=True)
@commands.has_role("The Holy Bell Pope")
async def declarewar(ctx):
    embed = discord.Embed(description= "🔔  @everyone WAR HAS BEEN DECLARED!!! 🔔", color=discord.Color.red())
    await ctx.send(embed=embed)
    print(f'🔔  {ctx.author.name} has declared war! 🔔')

#giverole command
@client.command(pass_context=True)
@commands.has_role("Staff")
async def giverole(ctx, member: discord.Member, role: discord.Role):
    await ctx.send(f"{member.name} has been given the role: {role.name}", delete_after=30.0)

#kick command
@client.command(pass_context=True)
@commands.has_role("Staff")
async def kick(ctx, member : discord.Member):
    await member.kick(reason="You have sinned")
    await ctx.send("kicked " + member.mention)

#ban command "GET OUTTA HERE"
@client.command(pass_context=True)
@commands.has_role("Staff")
async def ban(ctx, member: discord.Member):
    await member.ban(reason="GET OUTTA HERE")
    await ctx.send(f"{member.name} has been banned")

#mute command
@client.command(pass_context=True)
@commands.has_role("Staff")
async def mute(ctx, member: discord.Member,time):
    muted_role=discord.utils.get(ctx.guild.roles, name="Muted")
    time_convert = {"s":1, "m":60, "h":3600,"d":86400}
    tempmute= int(time[0]) * time_convert[time[-1]]
    await ctx.message.delete()
    await member.add_roles(muted_role)
    print(f"✅ {member.display_name}#{member.discriminator} muted successfully")
    embed = discord.Embed(description= f"✅ **{member.display_name}#{member.discriminator} muted successfully**", color=discord.Color.red())
    await ctx.send(embed=embed, delete_after=5)
    await asyncio.sleep(tempmute)
    await member.remove_roles(muted_role)

#unmute command
@client.command(pass_context=True)
@commands.has_role("Staff")
async def unmute(ctx, member: discord.Member):
    muted_role=discord.utils.get(ctx.guild.roles, name="Muted")
    await member.remove_roles(muted_role)
    embed = discord.Embed(description= f"✅ **{member.display_name}#{member.discriminator} is now unmuted**", color=discord.Color.green())
    await ctx.send(embed=embed, delete_after=10)
    await ctx.message.delete()
    print(f"✅  {member.display_name}#{member.discriminator} has been unmuted")

#this is what keeps the discord bot alive
keep_alive()
#this is what connects the bot

client.run('Put your token here')
