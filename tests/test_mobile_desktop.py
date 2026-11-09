"""Registered-device desktop control, using fake SSH and no models or real input."""
import base64
import hashlib
import io
import json
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image

import core
import mobile_service as mobile
from test_mobile_service import backend, login, ORIGIN, COOKIE


@pytest.fixture(autouse=True)
def synthetic_mobile_origin(monkeypatch):
    monkeypatch.setenv('CLAUDE_BRIDGE_ORIGIN', ORIGIN)


@pytest.fixture
def viewer(backend, monkeypatch):
    calls, receipts = [], {}
    options = {'timeout': False, 'state': 'applied', 'frame_override': {}}
    jpeg = io.BytesIO()
    Image.new('RGB', (16, 9), 'navy').save(jpeg, format='JPEG')
    original = backend.remote.action
    def action(name, **values):
        if not name.startswith('desktop_'):
            return original(name, **values)
        calls.append((name, dict(values)))
        if name == 'desktop_frame':
            now = time.time()
            return {'session_id': 'a' * 32, 'frame_id': uuid.uuid4().hex, 'captured_at': now, 'expires_at': now + 45,
                'mime': 'image/jpeg', 'image_base64': base64.b64encode(jpeg.getvalue()).decode(),
                'width': 16, 'height': 9, 'desktop_width': 1920, 'desktop_height': 1080} | options['frame_override']
        if name == 'desktop_input':
            fingerprint = hashlib.sha256(json.dumps([values['viewer_id'], values['session_id'], values['frame_id'], values['event']],
                ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            result = {'request_id': values['request_id'], 'fingerprint': fingerprint, 'state': options['state'],
                'matched': True, 'completed': options['state'] != 'unknown', 'found': True, 'refresh_required': True}
            receipts[values['request_id']] = result
            if options['timeout']:
                raise core.BridgeError('Synthetic SSH timeout after native input')
            return result
        if name == 'desktop_input_status':
            return receipts.get(values['request_id'], {'found': False, 'matched': False})
        if name == 'desktop_close':
            return {'closed': True}
        raise AssertionError(name)
    monkeypatch.setattr(backend.remote, 'action', action)
    return SimpleNamespace(backend=backend, headers=login(backend), id=uuid.uuid4().hex, calls=calls,
                           receipts=receipts, options=options)


def post(viewer, action, body=None):
    return viewer.backend.client.post('/api/desktop/' + action, headers=viewer.headers,
                                      json={'viewer_id': viewer.id} | (body or {}))


def frame(viewer):
    response = post(viewer, 'frame')
    assert response.status_code == 200, response.text
    return response.json()


def input_body(snapshot, event=None):
    return {'session_id': snapshot['session_id'], 'frame_id': snapshot['frame_id'], 'request_id': uuid.uuid4().hex,
            'event': event or {'kind': 'click', 'x': .25, 'y': .75, 'button': 1, 'clicks': 1}}


def test_desktop_requires_registered_device_origin_and_csrf_before_ssh(backend):
    viewer_id = uuid.uuid4().hex
    assert backend.client.post('/api/desktop/frame', json={'viewer_id': viewer_id}).status_code == 401
    headers = login(backend)
    assert backend.client.post('/api/desktop/frame', json={'viewer_id': viewer_id}).status_code == 403
    assert backend.client.post('/api/desktop/frame', headers=headers | {'Origin': 'https://foreign.example.test'},
                               json={'viewer_id': viewer_id}).status_code == 403
    assert backend.remote.calls == []


def test_desktop_frame_is_read_only_no_cache_and_independent_of_reply_lock(viewer):
    service = viewer.backend.service
    with service.remote_lock:
        snapshot = frame(viewer)
    assert snapshot['mime'] == 'image/jpeg' and snapshot['desktop_width'] == 1920
    assert viewer.calls[0][0] == 'desktop_frame'
    assert not viewer.backend.translator.translations and not viewer.backend.translator.reply_translations
    assert viewer.backend.service.history() == []
    response = post(viewer, 'frame')
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['X-Frame-Options'] == 'DENY'
    assert 'frame-ancestors' in response.headers['Content-Security-Policy']


def test_desktop_sends_fresh_guards_without_repeating_known_image_bytes(viewer):
    first = frame(viewer)
    assert first['image_base64'] and first['unchanged'] is False
    second = post(viewer, 'frame', {'image_hash': first['image_hash']}).json()
    assert second['image_base64'] == '' and second['unchanged'] is True
    assert second['image_hash'] == first['image_hash'] and second['frame_id'] != first['frame_id']
    assert post(viewer, 'input', input_body(second)).json()['state'] == 'applied'
    fresh_page = frame(viewer)
    assert fresh_page['image_base64'] and fresh_page['unchanged'] is False
    assert post(viewer, 'frame', {'image_hash': 'wrong'}).status_code == 422


def test_desktop_viewer_cannot_be_taken_over_by_another_registered_device(viewer):
    snapshot = frame(viewer)
    first = post(viewer, 'input', input_body(snapshot)).json()
    viewer.headers = login(viewer.backend)
    before = len(viewer.calls)
    assert post(viewer, 'frame').status_code == 403
    assert post(viewer, 'close').status_code == 403
    assert post(viewer, 'status', {'request_id': first['request_id'], 'fingerprint': first['fingerprint']}).status_code == 403
    assert len(viewer.calls) == before


def test_expired_or_revoked_device_cannot_read_existing_desktop(viewer):
    frame(viewer)
    with viewer.backend.service.database() as db:
        db.execute('UPDATE devices SET revoked=1')
    assert post(viewer, 'frame').status_code == 401
    assert post(viewer, 'close').status_code == 401
    assert len(viewer.calls) == 1


def test_desktop_input_runs_once_and_requires_new_frame_after_operation(viewer):
    snapshot = frame(viewer)
    payload = input_body(snapshot)
    first = post(viewer, 'input', payload)
    assert first.status_code == 200 and first.json()['state'] == 'applied'
    assert post(viewer, 'input', payload).json() == first.json()
    assert len([call for call in viewer.calls if call[0] == 'desktop_input']) == 1
    changed = payload | {'event': {'kind': 'key', 'key': 'Return'}}
    assert post(viewer, 'input', changed).status_code == 409
    assert post(viewer, 'input', input_body(snapshot)).status_code == 409
    assert post(viewer, 'input', input_body(frame(viewer), {'kind': 'key', 'key': 'Return'})).json()['state'] == 'applied'
    with viewer.backend.service.database() as db:
        assert db.execute('SELECT COUNT(*) FROM desktop_inputs').fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0


def test_unknown_input_only_queries_original_receipt_without_repeating_input(viewer):
    payload = input_body(frame(viewer), {'kind': 'text', 'text': 'Temporary input, not a chat draft'})
    viewer.options['timeout'] = True
    first = post(viewer, 'input', payload)
    assert first.status_code == 200 and first.json()['state'] == 'unknown'
    assert post(viewer, 'input', payload).json()['state'] == 'unknown'
    # The browser can recover by its saved viewer/id even when the timed-out
    # request never delivered the server-computed canonical fingerprint.
    status = post(viewer, 'status', {'request_id': payload['request_id'], 'fingerprint': ''})
    assert status.status_code == 200 and status.json()['state'] == 'applied'
    assert len([call for call in viewer.calls if call[0] == 'desktop_input']) == 1
    reads = [values for name, values in viewer.calls if name == 'desktop_input_status']
    assert len(reads) == 1 and reads[0]['request_id'] == payload['request_id']
    assert reads[0]['fingerprint'] == status.json()['fingerprint']
    assert post(viewer, 'status', {'request_id': payload['request_id'], 'fingerprint': '0' * 64}).status_code == 409
    with viewer.backend.service.database() as db:
        columns = [row[1] for row in db.execute('PRAGMA table_info(desktop_inputs)')]
        assert 'payload' not in columns and 'event' not in columns and 'text' not in columns
    assert viewer.backend.translator.translations == []


def test_native_not_applied_receipt_is_confirmed_without_automatic_retry(viewer):
    viewer.options['state'] = 'not_applied'
    receipt = post(viewer, 'input', input_body(frame(viewer))).json()
    assert receipt['state'] == 'not_applied' and receipt['completed'] is True
    assert len([name for name, _ in viewer.calls if name == 'desktop_input']) == 1


def test_input_status_recovers_original_receipt_after_backend_restart(viewer):
    payload = input_body(frame(viewer))
    viewer.options['timeout'] = True
    assert post(viewer, 'input', payload).json()['state'] == 'unknown'
    with viewer.backend.service.database() as db:
        row = db.execute('SELECT device_id FROM desktop_inputs WHERE id=?', (payload['request_id'],)).fetchone()
    restarted = mobile.MobileBridge(viewer.backend.directory, ssh_factory=viewer.backend.remote.client,
                                   translator_factory=viewer.backend.translator.factory)
    result = restarted.desktop_status(row['device_id'], {'viewer_id': viewer.id, 'request_id': payload['request_id'], 'fingerprint': ''})
    assert result['state'] == 'applied'
    assert len([name for name, _ in viewer.calls if name == 'desktop_input']) == 1


def test_closing_viewer_while_screenshot_is_running_discards_late_frame(viewer, monkeypatch):
    remote = viewer.backend.remote.action
    def close_during_frame(name, **values):
        result = remote(name, **values)
        if name == 'desktop_frame':
            entry = viewer.backend.service.desktop_viewers[viewer.id]
            viewer.backend.service.desktop_close(entry['device_id'], viewer.id)
        return result
    monkeypatch.setattr(viewer.backend.remote, 'action', close_during_frame)
    assert post(viewer, 'frame').status_code == 409
    assert viewer.backend.service.desktop_viewers[viewer.id]['closed']
    assert not viewer.backend.service.desktop_viewers[viewer.id]['frames']


def test_busy_input_is_known_not_started_and_frame_busy_is_read_only(viewer):
    snapshot = frame(viewer)
    service = viewer.backend.service
    with service.remote_lock:
        assert post(viewer, 'input', input_body(snapshot)).status_code == 409
    with service.desktop_frame_lock:
        assert post(viewer, 'frame').status_code == 409
    assert len(viewer.calls) == 1
    with service.database() as db:
        assert db.execute('SELECT COUNT(*) FROM desktop_inputs').fetchone()[0] == 0


def test_stale_frame_close_and_changed_connection_prevent_new_input(viewer):
    snapshot = frame(viewer)
    service = viewer.backend.service
    service.desktop_viewers[viewer.id]['frames'][snapshot['frame_id']]['expires'] = time.monotonic() - 1
    assert post(viewer, 'input', input_body(snapshot)).status_code == 409
    snapshot = frame(viewer)
    assert post(viewer, 'close').json() == {'closed': True}
    assert post(viewer, 'input', input_body(snapshot)).status_code == 409
    assert post(viewer, 'frame').status_code == 409
    another = uuid.uuid4().hex
    viewer.id = another
    snapshot = frame(viewer)
    config = service.config()
    config['ssh']['host'] = 'another-synthetic-host'
    core.atomic_json(viewer.backend.directory / 'config.json', config)
    assert post(viewer, 'input', input_body(snapshot)).status_code == 409
    assert not [call for call in viewer.calls if call[0] == 'desktop_input']


@pytest.mark.parametrize('event', [
    {'kind': 'key', 'key': 'arbitrary-shell-command'}, {'kind': [], 'text': 'bad'},
    {'kind': 'click', 'x': -1, 'y': .5, 'button': 1, 'clicks': 1},
    {'kind': 'click', 'x': True, 'y': .5, 'button': 1, 'clicks': 1},
    {'kind': 'text', 'text': 'a' * 20001}, {'kind': 'text', 'text': 'bad\x00text'},
    {'kind': 'key', 'key': 'Return', 'command': 'unexpected'},
    {'kind': 'scroll', 'x': .5, 'y': .5, 'delta_x': 0, 'delta_y': 1201},
    {'kind': 'drag', 'x': .2, 'y': .2, 'to_x': 2, 'to_y': .5, 'button': 1},
])
def test_desktop_rejects_invalid_input_vocabulary_without_ssh(viewer, event):
    snapshot = frame(viewer)
    assert post(viewer, 'input', input_body(snapshot, event)).status_code == 400
    assert len(viewer.calls) == 1


@pytest.mark.parametrize('override', [
    {'mime': 'image/svg+xml'}, {'width': 1601}, {'height': 0}, {'desktop_width': True},
    {'image_base64': 'not base64'}, {'image_base64': base64.b64encode(b'bad-jpeg').decode()},
    {'captured_at': 1, 'expires_at': 46}, {'session_id': ''},
])
def test_desktop_rejects_invalid_or_expired_frames(viewer, override):
    viewer.options['frame_override'] = override
    assert post(viewer, 'frame').status_code == 400
    assert not viewer.backend.service.desktop_viewers[viewer.id]['frames']


def test_desktop_public_shell_and_protected_screen_apis(viewer, tmp_path, monkeypatch):
    (tmp_path / 'desktop.html').write_text('<!doctype html><title>Desktop</title>', encoding='utf-8')
    (tmp_path / 'desktop.js').write_text('console.log("synthetic");', encoding='utf-8')
    (tmp_path / 'desktop.css').write_text('body {}', encoding='utf-8')
    monkeypatch.setattr(mobile, 'WEB', tmp_path)
    assert viewer.backend.client.get('/desktop').status_code == 200
    assert viewer.backend.client.get('/desktop.js').status_code == 200
    assert viewer.backend.client.get('/desktop.css').status_code == 200
    assert viewer.backend.client.get('/private.json').status_code == 404
    viewer.backend.client.cookies.clear()
    assert viewer.backend.client.get('/desktop').status_code == 200
    assert post(viewer, 'frame').status_code == 401
    assert viewer.calls == []
