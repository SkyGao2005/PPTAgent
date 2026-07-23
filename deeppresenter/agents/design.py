from collections.abc import AsyncGenerator
from pathlib import Path

from deeppresenter.agents.agent import Agent
from deeppresenter.utils.typings import ChatMessage, ContextLayer, InputRequest, Role


class Design(Agent):
    async def loop(
        self,
        req: InputRequest,
        markdown_file: str,
    ) -> AsyncGenerator[str | ChatMessage, None]:
        (self.workspace / "slides").mkdir(exist_ok=True)
        template_context_path = getattr(req, "template_context_path", None)
        template_overview = "Not provided"
        if template_context_path:
            overview_path = Path(template_context_path) / "overview.md"
            if overview_path.is_file():
                # The context-pack builder already projects this file to a
                # bounded payload. Read it whole so JSON/identity fields are
                # never cut at an arbitrary character boundary; model
                # preflight will fail explicitly if the configured token
                # budget is smaller than the pinned overview.
                template_overview = overview_path.read_text(encoding="utf-8")
        chat_kwargs = {
            "markdown_file": markdown_file,
            "prompt": req.designagent_prompt,
            "template_context_path": template_context_path or "Not provided",
            "template_context_overview": template_overview,
        }
        if len(self.chat_history) == 1:
            self.add_context_message(
                ChatMessage(
                    role=Role.USER,
                    content=self.prompt.render(**chat_kwargs),
                ),
                ContextLayer.PINNED,
                template_context=bool(template_context_path),
            )
        while True:
            agent_message = await self.action(**chat_kwargs)
            yield agent_message
            outcome = await self.execute(self.chat_history[-1].tool_calls)
            if isinstance(outcome, list):
                for item in outcome:
                    yield item
            else:
                break

        yield outcome
