"""Portable edge downloader. The host needs only install_menu(window, tools_menu)."""


def install_menu(window, tools_menu):
    """Install once; import the dialog only when opened."""
    from .integration import install_menu as install
    return install(window, tools_menu)
