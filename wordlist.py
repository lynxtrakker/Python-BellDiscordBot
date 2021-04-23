wordfilter = ["hi", "fuck", "shit", "ass", "bitch", "slut", "$lut", "a$$", "nigger", "nigga", "bastard", "biatch", "arse", "pussy", "coochie", "anal", "hell", "homo", "fag", "faggot", "feck", "damn", "sex", "scrotum", "sh1t", "tit", "vagina", "whore", "wank", "wh0re"]

'''@client.event
async def on_message(message):

    if message.author == client.user:
        return
    
    
    if message.content.lower().replace(' ', '') in wordfilter:
        if message.author.id == 399:
            return

        else:
            await message.channel.send("Don't say that!", delete_after=5.0)
            await message.delete()
            print(message.author.name + " or " + str(message.author.id) + " has been warned!")'''