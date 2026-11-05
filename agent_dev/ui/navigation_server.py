from agent_dev.ui.local_web import LocalWeb
from agent_dev.ui.shared_assets import NAVIGATION_ASSETS


class NavigationWeb(LocalWeb):
    prefix = page = "navigation"

    @classmethod
    def matches(cls, path):
        return path in NAVIGATION_ASSETS
