"""Post the scheduled Ray welcome once, then pin that message until the stream ends."""
import logging
import re
import sqlite3
import time

import requests

from ravora.twitch_chat import TwitchError
from ravora.twitch_identity import channel_login
from ravora.twitch_schedule_api import HELIX, PIN_SCOPES, channel_grant, headers, query

LOG = logging.getLogger(__name__)
WELCOME = (
    "I’m Ray, an AI VTuber. Say hi in English or Japanese! I reply out loud. / "
    "AI VTuberのRayです。「こんにちは」だけでも大歓迎！ 日本語でも英語でも、声でお返事します。"
)


# Both the claim and the final pre-write check require the same currently owned broadcast.
BOUND_SLOT = """FROM twitch_schedule_runs r JOIN studio_sessions s USING(session)
    WHERE r.scheduled_start=? AND r.arm=? AND r.session=? AND r.result='claimed' AND r.finished_at IS NULL
      AND s.platform='twitch' AND s.stream_id=? AND s.identity_conflict=0 AND s.stopped IS NULL
      AND NOT EXISTS (SELECT 1 FROM twitch_welcome_pins w
          WHERE w.scheduled_start=r.scheduled_start AND w.stream_id!=s.stream_id)"""


def current_pin(http, auth, target):
    rows = query(http, auth, 'chat/pins', target)['data']
    if not rows:
        return None
    if (len(rows) != 1 or rows[0].get('broadcaster_id') != target['broadcaster_id']
            or not rows[0].get('message_id') or 'ends_at' not in rows[0]):
        raise TwitchError('TWITCH_PIN_RESPONSE_INVALID')
    return rows[0]


class ScheduledWelcome:
    def __init__(self, store, directory, slot, session, stop):
        self.store = store
        self.directory = directory
        self.slot = slot
        self.session = session
        self.stop = stop
        self.finished = False

    def _record(self):
        with self.store.connect() as db:
            return db.execute('SELECT state,message_id FROM twitch_welcome_pins WHERE scheduled_start=?',
                              (self.slot.start,)).fetchone()

    def is_announcement(self, message_id, text):
        """Called only for the broadcaster's own chat, including EventSub arriving before the POST reply."""
        if text != WELCOME:
            return False
        try:
            row = self._record()
        except sqlite3.Error:
            LOG.warning('TWITCH_WELCOME_INTAKE_LOOKUP_FAILED')
            return True  # Only the broadcaster's exact scheduled welcome reaches this lookup.
        if row is None:
            return False
        state, sent_id = row
        return sent_id == message_id if sent_id else state in ('sending', 'send_unknown')

    def _reserve(self, stream, state):
        with self.store.connect() as db:
            return db.execute(f"""INSERT INTO twitch_welcome_pins(scheduled_start,stream_id,state,updated)
                SELECT r.scheduled_start,s.stream_id,?,? {BOUND_SLOT} ON CONFLICT DO NOTHING""",
                (state, time.time(), self.slot.start, self.slot.arm, self.session, stream)).rowcount == 1


    def _change(self, old, new, message_id=None):
        with self.store.connect() as db:
            return db.execute("""UPDATE twitch_welcome_pins SET state=?,updated=?,message_id=COALESCE(?,message_id)
                WHERE scheduled_start=? AND state=?""",
                (new,time.time(),message_id,self.slot.start,old)).rowcount == 1

    def _active(self, http, auth, identity, stream):
        if self.stop.is_set() or not self.slot.start <= time.time() < self.slot.end:
            return False
        with self.store.connect() as db:
            binding = db.execute(f'SELECT 1 {BOUND_SLOT}',
                (self.slot.start, self.slot.arm, self.session, stream)).fetchone()
        if not binding:
            return False
        rows = query(http, auth, 'streams', {'user_id':identity})['data']
        return (len(rows) == 1 and rows[0].get('user_id') == identity and rows[0].get('user_login') == channel_login()
                and rows[0].get('type') == 'live' and rows[0].get('id') == stream
                and not self.stop.is_set() and self.slot.start <= time.time() < self.slot.end)

    def _send(self, http, auth, target, stream):
        if not self._reserve(stream, 'sending'):
            return None
        # A timeout or an invalid success body leaves delivery unknown; never repeat the POST.
        try:
            response = http.post(HELIX+'chat/messages', headers=auth,
                                 json={'broadcaster_id':target['broadcaster_id'], 'sender_id':target['moderator_id'],
                                       'message':WELCOME}, timeout=5, allow_redirects=False)
            if response.status_code != 200:
                state = 'send_rejected' if 400 <= response.status_code < 500 else 'send_unknown'
                self._change('sending', state)
                return None
            sent, = response.json()['data']
            if sent.get('is_sent') is False:
                self._change('sending', 'send_rejected')
                return None
            message_id = sent.get('message_id')
            if sent.get('is_sent') is not True or not isinstance(message_id,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}',message_id):
                raise ValueError('invalid send response')
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
            self._change('sending', 'send_unknown')
            return None
        return message_id if self._change('sending', 'sent', message_id) else None

    def _apply(self, http, stream):
        record = self._record()
        if record and record[0] not in ('sent', 'pin_dispatched'):
            return True
        oauth = channel_grant(self.directory/'pin-oauth.json', http, PIN_SCOPES)
        identity = oauth.user_id
        auth = headers(oauth)
        target = {'broadcaster_id':identity,'moderator_id':identity}
        if not self._active(http, auth, identity, stream):
            return False
        pin = current_pin(http, auth, target)
        if record is None:
            if pin or query(http, auth, 'shared_chat/session', {'broadcaster_id':identity})['data']:
                self._reserve(stream, 'skipped_existing_pin' if pin else 'skipped_shared_chat')
                return True
            if not self._active(http, auth, identity, stream):
                return False
            message_id = self._send(http, auth, target, stream)
            if message_id is None:
                return True
            state = 'sent'
            pin = current_pin(http, auth, target)
        else:
            state, message_id = record
        if pin:
            if pin['message_id'] == message_id and pin['ends_at'] is None:
                self._change(state, 'pinned')
            else:
                self._change(state, 'skipped_existing_pin')
            return True
        if state == 'pin_dispatched':
            # Reconcile an ambiguous PUT by GET only. Respect subsequent manual unpins.
            return False
        if not self._active(http, auth, identity, stream) or not self._change('sent', 'pin_dispatched'):
            return False
        # Omit duration_seconds to pin to the end of this stream. send(pin=true) only lasts 20 minutes.
        response = http.put(HELIX+'chat/pins', headers=auth, params={**target,'message_id':message_id},
                            timeout=5, allow_redirects=False)
        if 400 <= response.status_code < 500 and response.status_code != 409:
            self._change('pin_dispatched','pin_rejected')
            return True
        pin = current_pin(http, auth, target)
        if pin and pin['message_id'] == message_id and pin['ends_at'] is None:
            self._change('pin_dispatched','pinned')
            return True
        return False

    def on_live(self, stream):
        if self.finished or self.stop.is_set() or not self.slot.start <= time.time() < self.slot.end:
            return
        try:
            with requests.Session() as http:
                http.trust_env = False
                self.finished = self._apply(http, stream)
            if self.finished:
                record = self._record()
                if record:
                    LOG.info('TWITCH_WELCOME_%s', record[0].upper())
        except TwitchError as exc:
            if 'REAUTHORIZE' in str(exc):
                self.finished = True
            LOG.warning('TWITCH_WELCOME_CHECK_INCOMPLETE %s', exc)
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError, OSError, sqlite3.Error):
            LOG.warning('TWITCH_WELCOME_CHECK_INCOMPLETE')
