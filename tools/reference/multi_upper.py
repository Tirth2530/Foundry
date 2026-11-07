from uagents import Context, Model


class Ask(Model):
    text: str


class Ans(Model):
    text: str


@agent.on_message(Ask, replies=Ans)
async def handle(ctx: Context, sender: str, msg: Ask):
    await ctx.send(sender, Ans(text=msg.text.upper()))
