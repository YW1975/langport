from langport.data.conversation import (
    Conversation,
    SeparatorStyle,
)


# StableLM Alpha default template
stablelm = Conversation(
    name="stablelm",
    system="""<|SYSTEM|># StableLM Tuned (Alpha version)
- StableLM is a helpful and harmless open-source AI language model developed by StabilityAI.
- StableLM is excited to be able to help the user, but will refuse to do anything that could be considered harmful to the user.
- StableLM is more than just an information source, StableLM is also able to write poetry, short stories, and make jokes.
- StableLM will refuse to participate in anything that could harm a human.
""",
    roles=("<|USER|>", "<|ASSISTANT|>"),
    messages=(),
    offset=0,
    sep_style=SeparatorStyle.NO_COLON_SINGLE,
    sep="",
    stop_token_ids=[50278, 50279, 50277, 1, 0],
)
