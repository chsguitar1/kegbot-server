import asyncore
import datetime
import Queue
import sys
import time

from pykeg.core import kb_common
from pykeg.core import util
from pykeg.core.net import kegnet_message
from pykeg.core.net import kegnet_server
from pykeg.external.gflags import gflags

FLAGS = gflags.FLAGS

### Base kegbot thread class

class CoreThread(util.KegbotThread):
  """ Convenience wrapper around a threading.Thread """
  def __init__(self, kb_env, name):
    util.KegbotThread.__init__(self, name)
    self._kb_env = kb_env

  ### Event listener methods
  def PostEvent(self, ev):
    if ev.name() == kb_common.KB_EVENT.QUIT:
      self._logger.info('got quit event, quitting')
      self.Quit()


class WatchdogThread(CoreThread):
  """Monitors all threads in _kb_env for crashes."""

  def run(self):
    fault_detected = False
    while not self._quit:
      if not fault_detected:
        for thr in self._kb_env.GetThreads():
          if not thr.hasStarted():
            continue
          if not self._quit and not thr.isAlive():
            self._logger.error('Thread %s died unexpectedly' % thr.getName())
            self._kb_env.GetEventHub().PublishEvent(kb_common.KB_EVENT.QUIT)
            fault_detected = True
            break
      time.sleep(1.0)


class EventHubServiceThread(CoreThread):
  """Handles all event dispatches for the event hub."""

  def run(self):
    hub = self._kb_env.GetEventHub()
    while not self._quit:
      hub.DispatchNextEvent(timeout=0.5)


class FlowMonitorThread(CoreThread):
  """Watches flows for idleness."""

  def run(self):
    hub = self._kb_env.GetEventHub()
    max_idle = datetime.timedelta(seconds=10)
    while not self._quit:
      time.sleep(2.0)
      flows = self._kb_env.GetFlowManager().GetActiveFlows()
      for flow in flows:
        if flow.GetIdleTime() > max_idle:
          self._IdleOutFlow(flow)

  def _IdleOutFlow(self, flow):
    self._logger.info("Idling flow: %s" % flow)
    msg = kegnet_message.TapIdleMessage()
    msg.tap_name = flow.GetTap().GetName()
    self._kb_env.GetEventHub().PublishEvent(kb_common.KB_EVENT.FLOW_DEV_IDLE,
        msg)

class AlarmManagerThread(CoreThread):

  def run(self):
    am = self._kb_env.GetAlarmManager()
    while not self._quit:
      alarm = am.WaitForNextAlarm(1.0)
      if alarm is not None:
        self._logger.info('firing alarm: %s' % alarm)
        event = alarm.event()
        self._kb_env.GetEventHub().PublishEvent(event)


class EventHandlerThread(CoreThread):
  """ Basic event handling thread. """
  def __init__(self, kb_env, name):
    CoreThread.__init__(self, kb_env, name)
    self._event_queue = Queue.Queue()
    self._event_handlers = set()
    self._all_event_map = {}

  def AddEventHandler(self, event_handler):
    self._event_handlers.add(event_handler)
    self._RefreshEventMap()

  def _RefreshEventMap(self):
    for svc in self._event_handlers:
      for event_type, callback_list in svc.GetEventHandlers().iteritems():
        if self._all_event_map.get(event_type) is None:
          self._all_event_map[event_type] = set()
        for cb in callback_list:
          self._all_event_map[event_type].add(cb)

  def run(self):
    while not self._quit:
      self._Step(timeout=0.5)

  def _Step(self, timeout=0.5):
    event = self._WaitForEvent(timeout)
    if event is not None:
      self._ProcessEvent(event)
    return event

  def PostEvent(self, event):
    self._event_queue.put(event)

  def _GetCallbacksForEvent(self, event):
    name = event.name()
    return self._all_event_map.get(name, tuple())

  def _WaitForEvent(self, timeout=0.5):
    """ Block until an event is posted, then process it """
    try:
      ev = self._event_queue.get(timeout=timeout)
      return ev
    except Queue.Empty:
      return None

  def _ProcessEvent(self, event):
    """ Execute the event callback associated with the event, if present. """
    self._logger.debug('Processing event: %s' % event)
    if event.name() == kb_common.KB_EVENT.QUIT:
      self._logger.info('got quit event, quitting')
      self.Quit()
      return
    callbacks = self._GetCallbacksForEvent(event)
    for cb in callbacks:
      cb(event)

  def _FlushEvents(self):
    """ Process all events in the Queue immediately """
    while True:
      ev = self._Step(timeout=0.5)
      if ev is None:
        break


### Service threads

class NetProtocolThread(CoreThread):
  def __init__(self, kb_env, name):
    CoreThread.__init__(self, kb_env, name)
    self._server = kegnet_server.KegnetServer(self._kb_env)
    self._server.AddService(kegnet_server.KegnetBaseService)
    self._server.AddService(kegnet_server.KegnetFlowService)

  def run(self):
    self._logger.info("network thread started")
    self._server.RunServer()

  def Quit(self):
    self._server.StopServer()
    self._logger.info("network thread stopped")
