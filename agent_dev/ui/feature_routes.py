"""Mount project tools consistently in all existing web entry points."""

from agent_dev.ui.changes_server import ChangesWeb
from agent_dev.ui.design_reference_server import DesignReferenceWeb
from agent_dev.ui.integration_server import IntegrationWeb
from agent_dev.ui.journey_server import JourneyWeb
from agent_dev.ui.messenger_server import MessengerWeb
from agent_dev.ui.review_server import ReviewWeb
from agent_dev.ui.wiki_server import WikiWeb
from agent_dev.ui.workflow_server import WorkflowWeb


class FeatureRoutes:
    def __init__(self, root):
        from agent_dev.ui.agent_server import AgentWeb
        from agent_dev.ui.navigation_server import NavigationWeb

        self.pages = [
            AgentWeb(root),
            WorkflowWeb(root),
            IntegrationWeb(root),
            DesignReferenceWeb(root),
            NavigationWeb(root),
            JourneyWeb(root),
            ChangesWeb(root),
            ReviewWeb(root),
            WikiWeb(root),
        ]
        # The app starts this page's Slack listener (autostart) when the settings say so.
        self.messenger = MessengerWeb(root)
        self.pages.append(self.messenger)
        from agent_dev.ui.source_server import SourceWeb

        self.pages.append(SourceWeb(root))

    @staticmethod
    def matches(path):
        from agent_dev.ui.agent_server import AgentWeb
        from agent_dev.ui.navigation_server import NavigationWeb
        from agent_dev.ui.source_server import SourceWeb

        return any(
            page.matches(path)
            for page in (
                AgentWeb,
                WorkflowWeb,
                IntegrationWeb,
                DesignReferenceWeb,
                SourceWeb,
                NavigationWeb,
                JourneyWeb,
                ChangesWeb,
                ReviewWeb,
                WikiWeb,
                MessengerWeb,
            )
        )

    def handle(self, handler):
        from urllib.parse import urlsplit

        path = urlsplit(handler.path).path
        for page in self.pages:
            if page.matches(path):
                page.handle(handler)
                return

    def close(self):
        for page in self.pages:
            page.close()
