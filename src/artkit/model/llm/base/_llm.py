# -----------------------------------------------------------------------------
# © 2024 Boston Consulting Group. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# -----------------------------------------------------------------------------

"""
Implementation of llm module.
"""

import copy
import logging

# imports
from abc import ABCMeta, abstractmethod
from typing import Any, Generic, Self, TypeVar, final

from pytools.api import appenddoc, inheritdoc, subsdoc

from ...base import ConnectorMixin, GenAIModel
from ...util import retry_with_exponential_backoff
from ..history import ChatHistory

log = logging.getLogger(__name__)


__all__ = [
    "ChatModel",
    "ChatModel",
    "ChatModelConnector",
    "CompletionModel",
    "CompletionModelConnector",
]

#
# Type variables
#

T_ChatModel = TypeVar("T_ChatModel", bound="ChatModel")
T_Client = TypeVar("T_Client")

#
# Class declarations
#


class CompletionModel(GenAIModel, metaclass=ABCMeta):
    """
    A Large Language Model (LLM) that generates text completions.
    """

    @abstractmethod
    async def get_completion(
        self, *, prompt: str, **model_params: dict[str, Any]
    ) -> str:
        """
        Generate a text completion for the given prompt.

        :param prompt: the prompt to pass to the completion model
        :param model_params: additional parameters for the completion model
        :return: the text generated by the completion model, excluding the prompt
        """


class CompletionModelConnector(
    CompletionModel, ConnectorMixin[T_Client], Generic[T_Client], metaclass=ABCMeta
):
    """
    A connector to a Large Language Model (LLM) that generates text completions.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Apply the retry strategy to the image_to_text method
        cls.get_completion = (  # type: ignore[method-assign]
            retry_with_exponential_backoff(cls.get_completion)  # type: ignore
        )


class ChatModel(GenAIModel, metaclass=ABCMeta):
    """
    A Large Language Model (LLM) that generates responses for text prompts.

    Uses an optional system prompt to set up the model.
    """

    @property
    @abstractmethod
    def system_prompt(self) -> str | None:
        """
        The system prompt used to set up the LLM system.
        """

    @abstractmethod
    def with_system_prompt(self, system_prompt: str) -> Self:
        """
        Set the system prompt for the LLM system.

        :param system_prompt: the system prompt to use
        :return: a new LLM system with the system prompt set
        """

    @abstractmethod
    async def get_response(
        self,
        message: str,
        *,
        history: ChatHistory | None = None,
        **model_params: dict[str, Any],
    ) -> list[str]:
        """
        Get a response, or multiple alternative responses, from the chat system.

        :param message: the user prompt to pass to the chat system
        :param history: the chat history preceding the message
        :param model_params: additional parameters for the chat system
        :return: the response or alternative responses generated by the chat system
        :raises RequestLimitException: if an error occurs while communicating
            with the chat system
        """


@inheritdoc(match="""[see superclass]""")
class ChatModelConnector(
    ChatModel, ConnectorMixin[T_Client], Generic[T_Client], metaclass=ABCMeta
):
    """
    A chat system that connects to a client.
    """

    #: The system prompt used to set up the chat system.
    _system_prompt: str | None

    @subsdoc(
        # The pattern matches the row defining model_params, and move it to the end
        # of the docstring.
        pattern=r"(:param model_params: .*\n)((:?.|\n)*\S)(\n|\s)*",
        replacement=r"\2\1",
    )
    @appenddoc(to=ConnectorMixin.__init__)
    def __init__(
        self,
        *,
        model_id: str,
        api_key_env: str | None = None,
        initial_delay: float | None = None,
        exponential_base: float | None = None,
        jitter: bool | None = None,
        max_retries: int | None = None,
        system_prompt: str | None = None,
        **model_params: Any,
    ) -> None:
        """
        :param system_prompt: the system prompt to initialise the chat system with
          (optional)
        """
        super().__init__(
            model_id=model_id,
            api_key_env=api_key_env,
            initial_delay=initial_delay,
            exponential_base=exponential_base,
            jitter=jitter,
            max_retries=max_retries,
            **model_params,
        )
        self._system_prompt = system_prompt

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Apply the retry strategy to the image_to_text method
        cls.get_response = (  # type: ignore[method-assign]
            retry_with_exponential_backoff(cls.get_response)  # type: ignore
        )

    @property
    @final
    def system_prompt(self) -> str | None:
        """[see superclass]"""
        return self._system_prompt

    def with_system_prompt(self, system_prompt: str) -> Self:
        """
        Set the system prompt for the LLM system.

        :param system_prompt: the system prompt to use
        :return: a new LLM system with the system prompt set
        """
        llm_copy = copy.copy(self)
        llm_copy._system_prompt = system_prompt
        return llm_copy
