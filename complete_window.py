from __future__ import annotations

from annotation_mixin import AnnotationMixin
from main_window import MainWindow as BaseMainWindow
from playback_mixin import PlaybackMixin


class MainWindow(PlaybackMixin, AnnotationMixin, BaseMainWindow):
    """Complete native window assembled from focused behavior mixins."""

    pass

