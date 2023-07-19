from superagi.llms.google_palm import GooglePalm
from superagi.llms.openai import OpenAi
from superagi.llms.replicate import Replicate


class ModelFactory:
    def __init__(self):
        self._creators = {}

    def register_format(self, model, creator):
        self._creators[model] = creator

    def get_model(self, model, **kwargs):
        creator = self._creators.get(model)
        if not creator:
            raise ValueError(model)
        return creator(**kwargs)


factory = ModelFactory()
factory.register_format("gpt-4", lambda **kwargs: OpenAi(model="gpt-4", **kwargs))
factory.register_format("gpt-4-32k", lambda **kwargs: OpenAi(model="gpt-4-32k", **kwargs))
factory.register_format("gpt-3.5-turbo-16k", lambda **kwargs: OpenAi(model="gpt-3.5-turbo-16k", **kwargs))
factory.register_format("gpt-3.5-turbo", lambda **kwargs: OpenAi(model="gpt-3.5-turbo", **kwargs))
factory.register_format("google-palm-bison-001", lambda **kwargs: GooglePalm(model='models/chat-bison-001', **kwargs))
factory.register_format("replicate-llama13b-v2-chat",
                        lambda **kwargs: Replicate(model='a16z-infra/llama13b-v2-chat',
                                                   version="df7690f1994d94e96ad9d568eac121aecf50684a0b0963b25a41cc40061269e5",
                                                   **kwargs))


def get_model(api_key, model="gpt-3.5-turbo", **kwargs):
    return factory.get_model(model, api_key=api_key, **kwargs)