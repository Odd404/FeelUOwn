import asyncio
import logging
import json
import sys
from functools import partial
from contextlib import contextmanager

from fuocore import LiveLyric, Library
from fuocore.dispatch import Signal
from fuocore.models import Resolver, reverse, resolve, \
    ResolverNotFound
from fuocore.playlist import PlaybackMode
from fuocore.pubsub import (
    Gateway as PubsubGateway,
    HandlerV1 as PubsubHandlerV1,
)

from .consts import APP_ICON, STATE_FILE
from .fm import FM
from .player import Player
from .plugin import PluginsManager
from .server import FuoServer
from .publishers import LiveLyricPublisher
from .request import Request
from .version import VersionManager
from .task import TaskManager

logger = logging.getLogger(__name__)


class App:
    """App 基类"""

    DaemonMode = 0x0001  # 开启 daemon
    GuiMode = 0x0010     # 显示 GUI
    CliMode = 0x0100     # 命令行模式

    def __init__(self, config):
        self.mode = config.MODE  # DEPRECATED: use app.config.MODE instead
        self.config = config
        self.initialized = Signal()
        self.about_to_shutdown = Signal()

        self.initialized.connect(lambda _: self.load_state(), weak=False)
        self.about_to_shutdown.connect(lambda _: self.dump_state(), weak=False)

    def show_msg(self, msg, *args, **kwargs):
        """在程序中显示消息，一般是用来显示程序当前状态"""
        # pylint: disable=no-self-use, unused-argument
        logger.info(msg)

    def get_listen_addr(self):
        return '0.0.0.0' if self.config.ALLOW_LAN_CONNECT else '127.0.0.1'

    def load_state(self):
        playlist = self.playlist
        player = self.player

        try:
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
        except FileNotFoundError:
            pass
        except json.decoder.JSONDecodeError:
            logger.exception('invalid state file')
        else:
            player.volume = state['volume']
            playlist.playback_mode = PlaybackMode(state['playback_mode'])
            songs = []
            for song in state['playlist']:
                try:
                    song = resolve(song)
                except ResolverNotFound:
                    pass
                else:
                    songs.append(song)
            playlist.init_from(songs)
            if songs and self.mode & App.GuiMode:
                self.ui.table_container.show_player_playlist()

            song = state['song']

            def before_media_change(old_media, media):
                if old_media is not None or playlist.current_song != song:
                    player.media_about_to_changed.disconnect(before_media_change)
                    player.set_play_range()
                    player.resume()

            if song is not None:
                try:
                    song = resolve(state['song'])
                except ResolverNotFound:
                    pass
                else:
                    player.media_about_to_changed.connect(before_media_change,
                                                          weak=False)
                    player.pause()
                    player.set_play_range(start=state['position'])
                    player.load_song(song)

    def dump_state(self):
        playlist = self.playlist
        player = self.player

        song = self.player.current_song
        if song is not None:
            song = reverse(song, as_line=True)
        # TODO: dump player.media
        state = {
            'playback_mode': playlist.playback_mode.value,
            'volume': player.volume,
            'state': player.state.value,
            'song': song,
            'position': player.position,
            'playlist': [reverse(song, as_line=True) for song in playlist.list()],
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)

    @contextmanager
    def create_action(self, s):  # pylint: disable=no-self-use
        """根据操作描述生成 Action (alpha)

        设计缘由：用户需要知道目前程序正在进行什么操作，进度怎么样，
        结果是失败或者成功。这里将操作封装成 Action。
        """
        show_msg = self.show_msg

        class ActionError(Exception):
            pass

        class Action:
            def set_progress(self, value):
                value = int(value * 100)
                show_msg(s + '...{}%'.format(value), timeout=-1)

            def failed(self, msg=''):
                raise ActionError(msg)

        show_msg(s + '...', timeout=-1)  # doing
        try:
            yield Action()
        except ActionError as e:
            show_msg(s + '...failed\t{}'.format(str(e)))
        except Exception as e:
            show_msg(s + '...error\t{}'.format(str(e)))  # error
            raise
        else:
            show_msg(s + '...done')  # done


def attach_attrs(app):
    """初始化 app 属性"""
    loop = asyncio.get_event_loop()
    app.library = Library()
    app.live_lyric = LiveLyric()
    player_kwargs = dict(
        audio_device=bytes(app.config.MPV_AUDIO_DEVICE, 'utf-8')
    )
    app.player = Player(app=app, **(player_kwargs or {}))
    app.playlist = app.player.playlist
    app.plugin_mgr = PluginsManager(app)
    app.request = Request()
    app.task_mgr = TaskManager(app, loop)
    app.fm = FM(app)

    if app.mode & (app.DaemonMode | app.GuiMode):
        app.version_mgr = VersionManager(app)

    if app.mode & app.DaemonMode:
        app.server = FuoServer(app)
        app.pubsub_gateway = PubsubGateway()
        app._ll_publisher = LiveLyricPublisher(app.pubsub_gateway)

    if app.mode & app.GuiMode:
        from feeluown.uimodels.provider import ProviderUiManager
        from feeluown.uimodels.playlist import PlaylistUiManager
        from feeluown.uimodels.my_music import MyMusicUiManager
        from feeluown.uimodels.collection import CollectionUiManager
        from feeluown.collection import CollectionManager

        from .browser import Browser
        from .hotkey import HotkeyManager
        from .image import ImgManager
        from .theme import ThemeManager
        from .tips import TipsManager
        from .ui import Ui

        # GUI 的一些辅助管理模块
        app.coll_mgr = CollectionManager(app)
        app.theme_mgr = ThemeManager(app)
        app.tips_mgr = TipsManager(app)
        app.hotkey_mgr = HotkeyManager(app)
        app.img_mgr = ImgManager(app)

        # GUI 组件的数据管理模块
        app.pvd_uimgr = ProviderUiManager(app)
        app.pl_uimgr = PlaylistUiManager(app)
        app.mymusic_uimgr = MyMusicUiManager(app)
        app.coll_uimgr = CollectionUiManager(app)

        app.browser = Browser(app)
        app.ui = Ui(app)
        app.show_msg = app.ui.magicbox.show_msg


def create_app(config):
    mode = config.MODE

    if mode & App.GuiMode:

        from PyQt5.QtGui import QIcon, QPixmap
        from PyQt5.QtWidgets import QApplication, QWidget

        from feeluown.compat import QEventLoop

        q_app = QApplication(sys.argv)
        q_app.setQuitOnLastWindowClosed(True)
        q_app.setApplicationName('FeelUOwn')

        app_event_loop = QEventLoop(q_app)
        asyncio.set_event_loop(app_event_loop)

        class GuiApp(QWidget):
            mode = App.GuiMode

            def __init__(self):
                super().__init__()
                self.setObjectName('app')
                QApplication.setWindowIcon(QIcon(QPixmap(APP_ICON)))

            def closeEvent(self, e):
                self.ui.mpv_widget.close()
                event_loop = asyncio.get_event_loop()
                event_loop.stop()

        class FApp(App, GuiApp):
            def __init__(self, config):
                App.__init__(self, config)
                GuiApp.__init__(self)

    else:
        FApp = App

    Signal.setup_aio_support()
    Resolver.setup_aio_support()
    app = FApp(config)
    attach_attrs(app)
    Resolver.library = app.library
    return app


def init_app(app):
    app.player.position_changed.connect(app.live_lyric.on_position_changed)
    app.playlist.song_changed.connect(app.live_lyric.on_song_changed)
    if app.mode & app.DaemonMode:
        app.live_lyric.sentence_changed.connect(app._ll_publisher.publish)

    app.plugin_mgr.scan()
    if app.mode & App.GuiMode:
        app.theme_mgr.initialize()
        app.tips_mgr.show_random_tip()
        app.coll_uimgr.initialize()
        app.browser.initialize()
        app.show()


def run_app(app):
    loop = asyncio.get_event_loop()

    if app.mode & (App.DaemonMode | App.GuiMode):
        loop.call_later(10, partial(loop.create_task, app.version_mgr.check_release()))

    if app.mode & App.DaemonMode:
        if sys.platform.lower() == 'darwin':
            try:
                from .global_hotkey_mac import MacGlobalHotkeyManager
            except ImportError as e:
                logger.warning("Can't start mac hotkey listener: %s", str(e))
            else:
                mac_global_hotkey_mgr = MacGlobalHotkeyManager()
                mac_global_hotkey_mgr.start()
        if sys.platform.lower() == 'linux':
            from feeluown.linux import run_mpris2_server
            run_mpris2_server(app)

        loop.create_task(app.server.run(app.get_listen_addr()))
        client_connected_cb = PubsubHandlerV1(app.pubsub_gateway).handle
        loop.create_task(asyncio.start_server(
            client_connected_cb,
            host=app.get_listen_addr(),
            port=23334,
            loop=loop))
    try:
        if not (app.config.MODE & (App.GuiMode | App.DaemonMode)):
            logger.warning('Fuo running with no daemon and no window')
        loop.run_forever()
    except KeyboardInterrupt:
        # NOTE: gracefully shutdown?
        pass
    finally:
        _shutdown_app(app)
        loop.stop()
        loop.close()


def run_app_once(app, future):
    loop = asyncio.get_event_loop()

    try:
        loop.run_until_complete(future)
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown_app(app)
        loop.stop()
        loop.close()


def _shutdown_app(app):
    app.about_to_shutdown.emit(app)
    app.player.stop()
    app.player.shutdown()
    Signal.teardown_aio_support()
