"""Wayland text delivery is bound to the active native page and releases its keys."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import desktop_remote as d

context='a'*64
target={'kind':'wayland','frame_id':'/org/a11y/atspi/accessible/frame','process_id':42,'context':context}
atspi=SimpleNamespace(STATE_ACTIVE=1,STATE_FOCUSED=2,STATE_ENABLED=3,STATE_EDITABLE=4)
frame=SimpleNamespace(parent=None,path=target['frame_id'],getRoleName=lambda:'frame',get_process_id=lambda:42,
                      getState=lambda:SimpleNamespace(contains=lambda value:True),clear_cache=Mock())
page=SimpleNamespace(parent=frame,getRoleName=lambda:'document web')
application=SimpleNamespace(get_process_id=lambda:42)

def rejected(call):
    try:call()
    except d.DesktopError:return
    raise AssertionError('Unverified native input was accepted')

with patch.object(d,'app',return_value=application),patch.object(d,'document',return_value=(page,{})), \
     patch.object(d,'initialize',return_value=atspi),patch.object(d,'showing',return_value=True), \
     patch.object(d,'page_context',return_value=context):
    assert d.native_input_frame()==target
    with patch.object(d,'window_id',side_effect=d.WindowUnavailable('No X11 client')):
        assert d.input_window()==target
    with patch.object(d,'window_id',side_effect=d.DesktopError('Duplicate X11 clients')), \
         patch.object(d,'native_input_frame') as fallback:
        rejected(d.input_window);fallback.assert_not_called()
    with patch.object(d,'window_id',side_effect=d.HiddenX11Window('Minimized X11 client')), \
         patch.object(d,'native_input_frame') as fallback:
        rejected(d.input_window);fallback.assert_not_called()
    with patch.object(frame,'get_process_id',return_value=99):rejected(d.native_input_frame)
    with patch.object(frame,'getState',return_value=SimpleNamespace(contains=lambda value:False)):
        rejected(d.native_input_frame)
    with patch.object(d,'showing',return_value=False):rejected(d.native_input_frame)

entry=SimpleNamespace(queryComponent=lambda:SimpleNamespace(grabFocus=Mock()),
                      getState=lambda:SimpleNamespace(contains=lambda value:True))
state={'context':context,'process_id':42,'draft':'','attachments':[],'dialogs':[], 'busy':False,
       'approval':False,'awaiting_input':False,'locked':False,'window_visible':True,'ready':True,'title':'Code'}
with patch.object(d,'status',return_value=state),patch.object(d,'input_window',return_value=target), \
     patch.object(d,'start') as launch:
    assert d.check_ready({'context':context,'for_send':True})['input_transport']=='wayland'
    launch.assert_not_called()
with patch.object(d,'initialize',return_value=atspi),patch.object(d,'activate',return_value=target), \
     patch.object(d,'visible_menus',return_value=[]),patch.object(d,'composer',return_value=entry), \
     patch.object(d,'require_native_focus') as focus,patch.object(d,'xcommand') as keys:
    assert d.focus_composer(context)==target
    focus.assert_called_once_with(target,context,'',allow_running=False);keys.assert_not_called()
    rejected(lambda:d.focus_composer('b'*64))
with patch.object(d,'status',return_value=state),patch.object(d,'native_input_frame',return_value=target), \
     patch.object(d,'composer',return_value=entry),patch.object(d,'initialize',return_value=atspi), \
     patch.object(d,'showing',return_value=True),patch.object(d,'text',return_value=''), \
     patch.object(d,'visible_menus',return_value=[]):
    d.require_native_focus(target,context,'')
    for change in ({'context':'b'*64},{'draft':'Other draft'},{'locked':True},{'approval':True},{'dialogs':['Chooser']},
                   {'awaiting_input':True},{'process_id':99}):
        with patch.object(d,'status',return_value={**state,**change}):
            rejected(lambda:d.require_native_focus(target,context,''))
    with patch.object(d,'native_input_frame',return_value={**target,'frame_id':target['frame_id']+'2'}):
        rejected(lambda:d.require_native_focus(target,context,''))
    with patch.object(d,'visible_menus',return_value=['Menu']):rejected(lambda:d.require_native_focus(target,context,''))

# Release every attempted press even if the compositor rejects a later press.
keyboard=d.NativeKeyboard.__new__(d.NativeKeyboard)
keyboard.owner=':1.2';keyboard.path='/session/own';keyboard.require_unlocked=Mock()
events=[]
def call(destination,path,interface,method,signature=None,arguments=()):
    if method=='GetNameOwner':return (':1.2',)
    events.append((method,arguments))
    if method=='NotifyKeyboardKeysym' and arguments==(ord('v'),True):raise d.DesktopError('Key rejected')
    return ()
keyboard.call=call
rejected(lambda:keyboard.key('Control_L+v'))
assert events==[('NotifyKeyboardKeysym',(0xffe3,True)),('NotifyKeyboardKeysym',(ord('v'),True)),
                ('NotifyKeyboardKeysym',(ord('v'),False)),('NotifyKeyboardKeysym',(0xffe3,False))]
keyboard.close();assert keyboard.path is None and events[-1][0]=='Stop'
keyboard.path='/session/own';keyboard.call=Mock(return_value=(':new.owner',))
rejected(lambda:keyboard.key('Return'))
assert keyboard.call.call_count==1

# Native paste uses the guarded compositor chord once, confirms exact entry
# text, restores the clipboard, and always closes its own keyboard session.
value='Continue the task.'
clipboard={'value':b'Existing clipboard'};draft={'value':''}
def process(arguments,**kwargs):
    if '-out' in arguments:return SimpleNamespace(stdout=clipboard['value'],returncode=0)
    clipboard['value']=kwargs['input'];return SimpleNamespace(returncode=0)
fake_keyboard=SimpleNamespace(key=Mock(side_effect=lambda key:draft.update(value=clipboard['value'].decode())),close=Mock())
with patch.object(d,'focus_composer',return_value=target),patch.object(d,'NativeKeyboard',return_value=fake_keyboard), \
     patch.object(d,'subprocess',SimpleNamespace(run=process,TimeoutExpired=TimeoutError,DEVNULL=None)), \
     patch.object(d,'require_native_focus') as focus,patch.object(d,'composer',return_value=entry), \
     patch.object(d,'text',side_effect=lambda node:draft['value']),patch.object(d,'pump_events'), \
     patch.object(d.time,'sleep'),patch.object(d,'xcommand',side_effect=AssertionError('X11 input used')):
    d.paste(value,context)
    fake_keyboard.key.assert_called_once_with('Control_L+v');fake_keyboard.close.assert_called_once()
    assert clipboard['value']==b'Existing clipboard' and draft['value']==value
    fake_keyboard.key.reset_mock();fake_keyboard.close.reset_mock();draft['value']=''
    focus.side_effect=d.DesktopError('Focus changed')
    rejected(lambda:d.paste(value,context))
    fake_keyboard.key.assert_not_called();fake_keyboard.close.assert_called_once()
    assert clipboard['value']==b'Existing clipboard'

print('Native Wayland input checks passed: active page/PID/frame, no restart, strict draft/focus guards, released compositor keys, once-only verified paste and clipboard/session cleanup')
