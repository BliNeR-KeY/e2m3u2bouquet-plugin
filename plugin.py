from __future__ import absolute_import, print_function

import errno
import os
import time

import six
import twisted.python.runtime
from Components.config import ConfigClock, ConfigEnableDisable, ConfigSelection, ConfigSelectionNumber, ConfigSubsection, ConfigText, ConfigYesNo, config
from Components.PluginComponent import plugins
from enigma import eEPGCache, eTimer
from Plugins.Plugin import PluginDescriptor
from Screens.MessageBox import MessageBox
from twisted.internet import threads

from . import _, log

try:
    import Plugins.Extensions.EPGImport.EPGConfig as EPGConfig
    import Plugins.Extensions.EPGImport.EPGImport as EPGImport
except ImportError:
    EPGImport = None
    EPGConfig = None

# Global variable
autoStartTimer = None
_session = None
providers_list = {}

# Set default configuration
config.plugins.e2m3u2b = ConfigSubsection()
config.plugins.e2m3u2b.debug = ConfigEnableDisable(default=False)
config.plugins.e2m3u2b.autobouquetupdate = ConfigYesNo(default=False)
config.plugins.e2m3u2b.scheduletype = ConfigSelection(default='interval', choices=['interval', 'fixed time'])
config.plugins.e2m3u2b.updateinterval = ConfigSelectionNumber(default=6, min=2, max=48, stepwidth=1)
config.plugins.e2m3u2b.schedulefixedtime = ConfigClock(default=0)
config.plugins.e2m3u2b.autobouquetupdateatboot = ConfigYesNo(default=False)
config.plugins.e2m3u2b.last_update = ConfigText()
config.plugins.e2m3u2b.extensions = ConfigYesNo(default=False)
config.plugins.e2m3u2b.mainmenu = ConfigYesNo(default=False)
config.plugins.e2m3u2b.do_epgimport = ConfigYesNo(default=True)


from . import e2m3u2bouquet
from .menu import E2m3u2b_Check, E2m3u2b_Menu


class AutoStartTimer:
    def __init__(self, session):
        self.session = session
        self.timer = eTimer()
        try:
            self.timer_conn = self.timer.timeout.connect(self.on_timer)
        except:
            self.timer.callback.append(self.on_timer)
        self.update()

    def get_wake_time(self):
        print('[e2m3u2b] AutoStartTimer -> get_wake_time', file=log)
        if config.plugins.e2m3u2b.autobouquetupdate.value:
            if config.plugins.e2m3u2b.scheduletype.value == 'interval':
                interval = int(config.plugins.e2m3u2b.updateinterval.value)
                nowt = time.time()
                # set next wakeup value to now + interval
                return int(nowt) + (interval * 60 * 60)
            elif config.plugins.e2m3u2b.scheduletype.value == 'fixed time':
                # convert the config clock to a time
                fixed_time_clock = config.plugins.e2m3u2b.schedulefixedtime.value
                now = time.localtime(time.time())

                fixed_wake_time = int(time.mktime((now.tm_year, now.tm_mon, now.tm_mday, fixed_time_clock[0],
                                                   fixed_time_clock[1], now.tm_sec, now.tm_wday, now.tm_yday, now.tm_isdst)))
                print(('fixed schedule time: ', time.asctime(time.localtime(fixed_wake_time))))
                return fixed_wake_time
        else:
            return -1

    def update(self):
        print('[e2m3u2b] AutoStartTimer -> update', file=log)
        self.timer.stop()
        wake = self.get_wake_time()
        nowt = time.time()
        now = int(nowt)

        if wake > 0:
            if wake <= now:
                # new wake time is in past set to the future time / interval
                if config.plugins.e2m3u2b.scheduletype.value == 'interval':
                    interval = int(config.plugins.e2m3u2b.updateinterval.value)
                    wake += interval * 60 * 60  # add interval in hours if wake up time is in past
                elif config.plugins.e2m3u2b.scheduletype.value == 'fixed time':
                    wake += 60 * 60 * 24  # add 1 day to fixed time if wake up time is in past

            next_wake = wake - now
            self.timer.startLongTimer(next_wake)
        else:
            wake = -1

        # print>> log, '[e2m3u2b] next wake up time {} (now={})'.format(wake, now)
        print('[e2m3u2b] next wake up time {} (now={})'.format(time.asctime(time.localtime(wake)), time.asctime(time.localtime(now))), file=log)
        return wake

    def on_timer(self):
        self.timer.stop()
        now = int(time.time())
        wake = now
        print('[e2m3u2b] on_timer occured at {}'.format(now), file=log)
        print('[e2m3u2b] Stating bouquet update because auto update bouquet schedule is enabled', file=log)

        if config.plugins.e2m3u2b.scheduletype.value == 'fixed time':
            wake = self.get_wake_time()

        # if close enough to wake time do bouquet update
        if wake - now < 60:
            try:
                start_update()
            except Exception as e:
                print("[e2m3u2b] on_timer Error:", e, file=log)
                if config.plugins.e2m3u2b.debug.value:
                    raise
        self.update()

    def get_status(self):
        print('[e2m3u2b] AutoStartTimer -> getStatus', file=log)


def start_update(epgimport=None):
    """Run m3u channel update
    """
    print('start_update called')

    e2m3u2b_config = e2m3u2bouquet.Config()
    if os.path.isfile(os.path.join(e2m3u2bouquet.CFGPATH, 'config.xml')):
        e2m3u2b_config.read_config(os.path.join(e2m3u2bouquet.CFGPATH, 'config.xml'))

        providers_to_process = []
        epgimport_sourcefiles = []

        for key, provider_config in six.iteritems(e2m3u2b_config.providers):
            if provider_config.enabled and not provider_config.name.startswith('Supplier Name'):
                providers_to_process.append(provider_config)
                epgimport_sourcefilename = os.path.join(e2m3u2bouquet.EPGIMPORTPATH, 'suls_iptv_{}.sources.xml'
                                                        .format(e2m3u2bouquet.get_safe_filename(provider_config.name)))
                epgimport_sourcefiles.append(epgimport_sourcefilename)

        if twisted.python.runtime.platform.supportsThreads():
            d = threads.deferToThread(start_process_providers, providers_to_process, e2m3u2b_config)
            d.addCallback(start_update_callback, epgimport_sourcefiles, int(time.time()), epgimport)
        else:
            start_process_providers(providers_to_process, e2m3u2b_config)
            start_update_callback(None, epgimport_sourcefiles, int(time.time()), epgimport)


def start_update_callback(result, epgimport_sourcefiles, start_time, epgimport=None):
    elapsed_secs = (int(time.time())) - start_time
    msg = 'Finished bouquet update in {}s'.format(str(elapsed_secs))
    e2m3u2bouquet.Status.message = msg
    print('[e2m3u2b] {}'.format(msg), file=log)

    # Attempt automatic epg import is option enabled and epgimport plugin detected
    if EPGImport and config.plugins.e2m3u2b.do_epgimport.value is True:
        if epgimport is None:
            epgimport = EPGImport.EPGImport(eEPGCache.getInstance(), lambda x: True)

        sources = [s for s in epgimport_sources(epgimport_sourcefiles)]
        sources.reverse()
        epgimport.sources = sources
        epgimport.onDone = epgimport_done
        epgimport.beginImport(longDescUntil=time.time() + (5 * 24 * 3600))


def start_process_providers(providers_to_process, e2m3u2b_config):
    providers_updated = False

    for provider_config in providers_to_process:
        provider = e2m3u2bouquet.Provider(provider_config)

        if int(time.time()) - int(provider.config.last_provider_update) > 21600:
            # wait at least 6 hours (21600s) between update checks
            providers_updated = provider.provider_update()

        print('[e2m3u2b] Starting backend script {}'.format(provider.config.name), file=log)
        provider.process_provider()
        print('[e2m3u2b] Finished backend script {}'.format(provider.config.name), file=log)

    if providers_updated:
        e2m3u2b_config.write_config()

    localtime = time.asctime(time.localtime(time.time()))
    config.plugins.e2m3u2b.last_update.value = localtime
    config.plugins.e2m3u2b.last_update.save()

    e2m3u2bouquet.reload_bouquets()


def epgimport_sources(sourcefiles):
    for sourcefile in sourcefiles:
        try:
            for s in EPGConfig.enumSourcesFile(sourcefile):
                yield s
        except Exception as e:
            print('[e2m3u2b] Failed to open epg source ', sourcefile, ' Error: ', e, file=log)


def epgimport_done(reboot=False, epgfile=None):
    print('[e2m3u2b] Automatic epg import finished', file=log)


def do_reset():
    """Reset bouquets and
    epg importer config by running the script uninstall method
    """
    print('do_reset called')
    e2m3u2bouquet.uninstaller()
    e2m3u2bouquet.reload_bouquets()


def main(session, **kwargs):
    check_cfg_folder()

    # Show message if EPG Import is not detected
    if not EPGImport:
        session.openWithCallback(open_menu(session), E2m3u2b_Check)
    else:
        open_menu(session)


def open_menu(session):
    session.open(E2m3u2b_Menu)


def check_cfg_folder():
    """Make config folder if it doesn't exist
    """
    try:
        os.makedirs(e2m3u2bouquet.CFGPATH)
    except OSError as e:      # race condition guard
        if e.errno != errno.EEXIST:
            print("[e2m3u2b] unable to create config dir:", e, file=log)
            if config.plugins.e2m3u2b.debug.value:
                raise


def done_configuring():
    """Check for new config values for auto start
    """
    print('[e2m3u2b] Done configuring', file=log)
    if autoStartTimer is not None:
        autoStartTimer.update()


def on_boot_start_check():
    """This will only execute if the
    config option autobouquetupdateatboot is true
    """
    now = int(time.time())
    # TODO Skip if there is an upcoming scheduled update
    print('[e2m3u2b] Stating bouquet update because auto update bouquet at start enabled', file=log)
    try:
        start_update()
    except Exception as e:
        print("[e2m3u2b] on_boot_start_check Error:", e, file=log)
        if config.plugins.e2m3u2b.debug.value:
            raise


def autostart(reason, session=None, **kwargs):
    # reason is 0 at start and 1 at shutdown
    # these globals need declared as they are reassigned here
    global autoStartTimer
    global _session

    print('[e2m3u2b] autostart {} occured at {}'.format(reason, time.time()), file=log)
    if reason == 0 and _session is None:
        if session is not None:
            _session = session
            if autoStartTimer is None:
                autoStartTimer = AutoStartTimer(session)
            if config.plugins.e2m3u2b.autobouquetupdateatboot.value:
                on_boot_start_check()
    else:
        print('[e2m3u2b] stop', file=log)


def get_next_wakeup():
    # don't enable waking from deep standby for now
    print('[e2m3u2b] get_next_wakeup', file=log)
    return -1


def menuHook(menuid):
    """ Called whenever a menu is created"""
    if menuid == "mainmenu":
        return [(plugin_name, quick_import_menu, plugin_name, 45)]
    return[]


def extensions_menu(session, **kwargs):
    """ Needed for the extension menu descriptor
    """
    main(session, **kwargs)


def quick_import_menu(session, **kwargs):
    session.openWithCallback(quick_import_callback, MessageBox, _("Update of channels will start. This may take a few minutes.\nProceed?"), MessageBox.TYPE_YESNO, timeout=15, default=True)


def quick_import_callback(confirmed):
    if not confirmed:
        return
    try:
        start_update()
    except Exception as e:
        print("[e2m3u2b] manual_update_callback Error:", e, file=log)
        if config.plugins.e2m3u2b.debug.value:
            raise


def update_extensions_menu(cfg_el):
    print('[e2m3u2b] update extensions menu', file=log)
    try:
        if cfg_el.value:
            plugins.addPlugin(extDescriptorQuick)
        else:
            plugins.removePlugin(extDescriptorQuick)
    except Exception as e:
        print('[e2m3u2b] Failed to update extensions menu: ', e, file=log)


def update_main_menu(cfg_el):
    print('[e2m3u2b] update main menu', file=log)
    try:
        if cfg_el.value:
            plugins.addPlugin(extDescriptorQuickMain)
        else:
            plugins.removePlugin(extDescriptorQuickMain)
    except Exception as e:
        print('[e2m3u2b] Failed to update main menu: ', e, file=log)


plugin_name = 'IPTV Bouquet Maker'
plugin_description = _("IPTV for Enigma2 - E2m3u2bouquet plugin")
print('[e2m3u2b] add notifier')
extDescriptor = PluginDescriptor(name=plugin_name, description=plugin_description, where=PluginDescriptor.WHERE_EXTENSIONSMENU, fnc=extensions_menu)
extDescriptorQuick = PluginDescriptor(name=plugin_name, description=plugin_description, where=PluginDescriptor.WHERE_EXTENSIONSMENU, fnc=quick_import_menu)
extDescriptorQuickMain = PluginDescriptor(name=plugin_name, description=plugin_description, where=PluginDescriptor.WHERE_MENU, fnc=menuHook)
config.plugins.e2m3u2b.extensions.addNotifier(update_extensions_menu, initial_call=False)
config.plugins.e2m3u2b.mainmenu.addNotifier(update_main_menu, initial_call=False)


def Plugins(**kwargs):
    result = [
        PluginDescriptor(
            name=plugin_name,
            description=plugin_description,
            where=[
                PluginDescriptor.WHERE_AUTOSTART,
                PluginDescriptor.WHERE_SESSIONSTART,
            ],
            fnc=autostart,
            wakeupfnc=get_next_wakeup
        ),
        PluginDescriptor(
            name=plugin_name,
            description=plugin_description,
            where=PluginDescriptor.WHERE_PLUGINMENU,
            icon='images/e2m3ubouquetlogo.png',
            fnc=main
        )  # ,
        #PluginDescriptor(
        #    name=plugin_name,
        #    description=plugin_description,
        #    where=PluginDescriptor.WHERE_MENU,
        #    icon='images/e2m3ubouquetlogo.png',
        #    fnc=menuHook
        #)
    ]

    if config.plugins.e2m3u2b.extensions.value:
        result.append(extDescriptorQuick)
    if config.plugins.e2m3u2b.mainmenu.value:
        result.append(extDescriptorQuickMain)
    return result
