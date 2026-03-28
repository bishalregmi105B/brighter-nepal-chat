"""
BrighterNepal Chat Service
==========================
A dedicated Flask-SocketIO server for real-time messaging.
Supports group chat rooms and live-class chat rooms.
Uses Redis as the message broker and presence store.

Rooms:
  group:<int>   – study group chat (students + admins)
  live:<int>    – live-class chat (tied to a live-class session)

Events (client → server):
  join            {room, token}              authenticate + enter room
  message         {room, text, image_url?}   send a text / image message
  typing          {room, is_typing}          broadcast typing indicator
  leave           {room}                     cleanly leave room

Events (server → client):
  history         [{...msg}]                 last 30 msgs on join
  message         {...msg}                   new message delivered to room
  typing          {user_id, name, is_typing} who is typing
  presence        {room, online_count}       room occupant count
  error           {msg}                      authentication / validation error
"""

# gevent monkey-patch MUST be first — before any other imports
from gevent import monkey; monkey.patch_all()

import os
import json
import time
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room, leave_room, rooms
from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import JWTManager, decode_token
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

# ────────────────────────────────────────────────────────────────────────────────
# App Setup
# ────────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

app.config['SECRET_KEY']            = os.environ.get('SECRET_KEY', 'brighter-nepal-super-secret-key-change-in-production')
app.config['JWT_SECRET_KEY']        = os.environ.get('JWT_SECRET_KEY', 'jwt-brighter-nepal-secret-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URI', 'sqlite:///../brighter-nepal-api/instance/brighter_nepal.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Connection Pool: prevent MySQL from being overwhelmed under simultaneous socket connections
_is_sqlite = os.environ.get('DATABASE_URI', 'sqlite://').startswith('sqlite')
if not _is_sqlite:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_size':     int(os.environ.get('DB_POOL_SIZE',    '10')),
        'max_overflow':  int(os.environ.get('DB_MAX_OVERFLOW', '20')),
        'pool_timeout':  int(os.environ.get('DB_POOL_TIMEOUT', '30')),
        'pool_recycle':  int(os.environ.get('DB_POOL_RECYCLE', '1800')),
        'pool_pre_ping': True,
    }

REDIS_URL    = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
FRONTEND_URL = os.environ.get('FRONTEND_URL', 'http://localhost:3000')
MESSAGE_CACHE_SIZE = 50   # number of messages to cache per room in Redis

CORS(app, origins=[FRONTEND_URL, 'http://localhost:3000', 'http://localhost:3001'])

db  = SQLAlchemy(app)
jwt = JWTManager(app)

# ── Try to connect Redis; fall back gracefully if unavailable ──────────────────
try:
    import redis as _redis
    _redis_client = _redis.from_url(REDIS_URL, decode_responses=True)
    _redis_client.ping()
    REDIS_AVAILABLE = True
    print('[chat] ✓ Redis connected')
except Exception as e:
    _redis_client = None
    REDIS_AVAILABLE = False
    print(f'[chat] ✗ Redis unavailable ({e}), using in-memory fallback')

# ── SocketIO with Redis message queue (or fallback eventlet) ───────────────────
if REDIS_AVAILABLE:
    socketio = SocketIO(
        app,
        cors_allowed_origins=[FRONTEND_URL, 'http://localhost:3000', 'http://localhost:3001'],
        message_queue=REDIS_URL,
        async_mode='gevent',
        ping_timeout=60,
        ping_interval=25,
    )
else:
    socketio = SocketIO(
        app,
        cors_allowed_origins=[FRONTEND_URL, 'http://localhost:3000', 'http://localhost:3001'],
        async_mode='gevent',
        ping_timeout=60,
        ping_interval=25,
    )

# ────────────────────────────────────────────────────────────────────────────────
# Shared Models (mirror of the main API models — read-only from this service)
# ────────────────────────────────────────────────────────────────────────────────
class User(db.Model):
    __tablename__ = 'users'
    id             = db.Column(db.Integer, primary_key=True)
    name           = db.Column(db.String(120))
    email          = db.Column(db.String(120))
    password_hash  = db.Column(db.String(256))
    plain_password = db.Column(db.String(100))
    whatsapp       = db.Column(db.String(30))
    paid_amount    = db.Column(db.Integer)
    joined_method  = db.Column(db.String(200))
    plan           = db.Column(db.String(20), default='trial')
    status         = db.Column(db.String(20), default='active')
    role           = db.Column(db.String(20), default='student')
    admin_note     = db.Column(db.Text, default='')
    group_id       = db.Column(db.Integer, nullable=True)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    session_token  = db.Column(db.String(64), nullable=True)
    device_count   = db.Column(db.Integer, default=0)


class Group(db.Model):
    __tablename__ = 'groups'
    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(200))
    description  = db.Column(db.Text, default='')
    member_count = db.Column(db.Integer, default=0)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)


class GroupMessage(db.Model):
    __tablename__ = 'group_messages'
    id         = db.Column(db.Integer, primary_key=True)
    group_id   = db.Column(db.Integer, nullable=False)
    user_id    = db.Column(db.Integer, nullable=False)
    text       = db.Column(db.Text, default='')
    image_url  = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user       = db.relationship('User', foreign_keys=[user_id],
                                 primaryjoin='GroupMessage.user_id == User.id')

    def to_dict(self):
        return {
            'id':          self.id,
            'group_id':    self.group_id,
            'user_id':     self.user_id,
            'sender_name': self.user.name if self.user else 'Unknown',
            'text':        self.text,
            'image_url':   self.image_url,
            'created_at':  self.created_at.isoformat(),
        }


class LiveClassMessage(db.Model):
    """Dedicated chat messages for live-class sessions."""
    __tablename__ = 'live_class_messages'
    id         = db.Column(db.Integer, primary_key=True)
    class_id   = db.Column(db.Integer, nullable=False)
    user_id    = db.Column(db.Integer, nullable=False)
    text       = db.Column(db.Text, default='')
    image_url  = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user       = db.relationship('User', foreign_keys=[user_id],
                                 primaryjoin='LiveClassMessage.user_id == User.id')

    def to_dict(self):
        return {
            'id':          self.id,
            'class_id':    self.class_id,
            'user_id':     self.user_id,
            'sender_name': self.user.name if self.user else 'Unknown',
            'text':        self.text,
            'image_url':   self.image_url,
            'created_at':  self.created_at.isoformat(),
        }


class LiveClass(db.Model):
    """Mirror of the main API LiveClass model — used to sync watcher counts."""
    __tablename__ = 'live_classes'
    id       = db.Column(db.Integer, primary_key=True)
    watchers = db.Column(db.Integer, default=0)


class PlatformSetting(db.Model):
    """Mirror of the main API PlatformSetting model — read-only key-value store."""
    __tablename__ = 'platform_settings'
    id         = db.Column(db.Integer, primary_key=True)
    key        = db.Column(db.String(100), unique=True, nullable=False)
    value      = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


def _sync_live_watchers(class_id: int, count: int):
    """Update live_classes.watchers to the current socket presence count."""
    def _do():
        with app.app_context():
            try:
                cls = LiveClass.query.get(class_id)
                if cls:
                    cls.watchers = max(0, count)
                    db.session.commit()
            except Exception as e:
                print(f'[watchers] sync failed for class {class_id}: {e}')
    from gevent import spawn
    spawn(_do)


# ────────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────────

# In-memory presence map when Redis is not available: { room: {sid: user_id} }
_mem_presence: dict[str, dict[str, int]] = {}

# In-memory JWT token cache to avoid DB lookup spikes: { token: (User_object, expiry_time) }
_token_cache: dict[str, tuple] = {}

# Per-room participant list: { room: { sid: {user_id, name, is_admin} } }
_room_users: dict[str, dict[str, dict]] = {}

# Per-room muted user set: { room: set(user_id) }
_muted_users: dict[str, set] = {}

# Rate limit tracking: { f"{room}:{user_id}": [timestamps...] }
_msg_timestamps: dict[str, list] = {}

# Cached rate limit settings: (max_count, window_secs, fetched_at)
_rate_limit_cache: tuple | None = None
_RATE_LIMIT_CACHE_TTL = 30  # seconds


def _get_rate_limit() -> tuple[int, int]:
    """Returns (max_messages, window_seconds) from DB/cache. Defaults: 20 msgs / 60s."""
    global _rate_limit_cache
    now = time.time()
    if _rate_limit_cache and now - _rate_limit_cache[2] < _RATE_LIMIT_CACHE_TTL:
        return _rate_limit_cache[0], _rate_limit_cache[1]
    try:
        rows = PlatformSetting.query.filter(
            PlatformSetting.key.in_(['chat_rate_limit_count', 'chat_rate_limit_window_secs'])
        ).all()
        settings = {r.key: r.value for r in rows}
        count  = int(settings.get('chat_rate_limit_count', '20') or '20')
        window = int(settings.get('chat_rate_limit_window_secs', '60') or '60')
    except Exception:
        count, window = 20, 60
    _rate_limit_cache = (count, window, now)
    return count, window


def _check_rate_limit(room: str, user_id: int) -> bool:
    """Returns True if message is allowed, False if rate-limited."""
    max_msgs, window = _get_rate_limit()
    if max_msgs <= 0:  # 0 means disabled
        return True
    key = f'{room}:{user_id}'
    now = time.time()
    timestamps = _msg_timestamps.get(key, [])
    timestamps = [t for t in timestamps if now - t < window]
    if len(timestamps) >= max_msgs:
        _msg_timestamps[key] = timestamps
        return False
    timestamps.append(now)
    _msg_timestamps[key] = timestamps
    return True


def _broadcast_user_list(room: str):
    """Broadcast deduplicated participant list to everyone in room."""
    seen: dict[int, dict] = {}
    for info in _room_users.get(room, {}).values():
        uid = info['user_id']
        if uid not in seen:
            seen[uid] = {'user_id': uid, 'name': info['name'], 'is_admin': info['is_admin']}
    muted = list(_muted_users.get(room, set()))
    socketio.emit('user_list', {
        'users': list(seen.values()),
        'muted_user_ids': muted,
    }, to=room)

def _get_user_from_token(token: str):
    """Decode JWT and return User object or None, utilizing a 60-second LRU memory cache."""
    now = time.time()
    if token in _token_cache:
        user_obj, exp = _token_cache[token]
        if now < exp:
            return user_obj
        else:
            del _token_cache[token]

    try:
        data = decode_token(token)
        sub  = data.get('sub')
        
        if isinstance(sub, str) and sub.startswith('{'):
            import json
            parsed = json.loads(sub)
            user_id = int(parsed.get('id', 0))
        else:
            user_id = int(sub)
            
        user = User.query.get(user_id)
        if not user:
            print(f"[_get_user_from_token] DB lookup failed for user_id={user_id}")
            return None
            
        _token_cache[token] = (user, now + 60) # Cache the user query strictly for 60 seconds
        return user
    except Exception as e:
        print(f"[_get_user_from_token] Exception: {type(e).__name__} - {e}")
        return None


def _room_key(room: str) -> str:
    return f'bn:room:{room}:sids'


def _presence_add(room: str, sid: str, user_id: int):
    if REDIS_AVAILABLE:
        _redis_client.hset(_room_key(room), sid, user_id)
        _redis_client.expire(_room_key(room), 86400)
    else:
        _mem_presence.setdefault(room, {})[sid] = user_id


def _presence_remove(room: str, sid: str):
    if REDIS_AVAILABLE:
        _redis_client.hdel(_room_key(room), sid)
    else:
        _mem_presence.get(room, {}).pop(sid, None)


def _presence_count(room: str) -> int:
    if REDIS_AVAILABLE:
        return _redis_client.hlen(_room_key(room))
    return len(_mem_presence.get(room, {}))


def _cache_message(room: str, msg_dict: dict):
    """Push message to Redis list; keep only last MESSAGE_CACHE_SIZE."""
    if not REDIS_AVAILABLE:
        return
    key = f'bn:room:{room}:msgs'
    flag = f'bn:room:{room}:loaded'
    _redis_client.rpush(key, json.dumps(msg_dict))
    _redis_client.ltrim(key, -MESSAGE_CACHE_SIZE, -1)
    _redis_client.set(flag, '1') # Forcefully assert data exists
    _redis_client.expire(key, 86400 * 7)        # keep 7 days
    _redis_client.expire(flag, 86400 * 7)


def _get_cached_messages(room: str) -> list[dict]:
    """Return last MESSAGE_CACHE_SIZE messages from Redis cache."""
    if not REDIS_AVAILABLE:
        return []
    key  = f'bn:room:{room}:msgs'
    raw  = _redis_client.lrange(key, 0, -1)
    return [json.loads(r) for r in raw]

def _is_room_loaded(room: str) -> bool:
    """Check if the DB history was already fetched into Redis."""
    if not REDIS_AVAILABLE:
        return False
    return bool(_redis_client.get(f'bn:room:{room}:loaded'))

def _mark_room_loaded(room: str):
    """Flag that DB historical loading is complete to prevent repeated empty DB lookups."""
    if not REDIS_AVAILABLE:
        return
    key = f'bn:room:{room}:loaded'
    _redis_client.set(key, '1')
    _redis_client.expire(key, 86400 * 7)


def _load_db_history(room: str, limit: int = 30) -> list[dict]:
    """Load message history from the database for a given room."""
    room_type, room_id_str = room.split(':', 1)
    room_id = int(room_id_str)

    if room_type == 'group':
        msgs = (
            GroupMessage.query
            .filter_by(group_id=room_id)
            .order_by(GroupMessage.created_at.desc())
            .limit(limit)
            .all()
        )
        return [m.to_dict() for m in reversed(msgs)]

    elif room_type == 'live':
        msgs = (
            LiveClassMessage.query
            .filter_by(class_id=room_id)
            .order_by(LiveClassMessage.created_at.desc())
            .limit(limit)
            .all()
        )
        return [m.to_dict() for m in reversed(msgs)]

    return []


def _save_message(room: str, user: User, text: str, image_url: str | None = None) -> dict:
    """Persist a message to the database and return its dict."""
    room_type, room_id_str = room.split(':', 1)
    room_id = int(room_id_str)

    if room_type == 'group':
        msg = GroupMessage(
            group_id  = room_id,
            user_id   = user.id,
            text      = text or '',
            image_url = image_url,
        )
    else:  # live
        msg = LiveClassMessage(
            class_id  = room_id,
            user_id   = user.id,
            text      = text or '',
            image_url = image_url,
        )

    db.session.add(msg)
    db.session.commit()
    return msg.to_dict()


# Compact in-memory cache for room access per user: {(user_id, room) -> (bool, expiry)}
_room_access_cache: dict[tuple, tuple] = {}


def _check_room_access(room: str, user: User) -> bool:
    """Admins can access any room. Students can only access their assigned group room.
    Caches access decisions for 120s to avoid per-event DB reads."""
    cache_key = (user.id, room)
    now = time.time()
    if cache_key in _room_access_cache:
        result, exp = _room_access_cache[cache_key]
        if now < exp:
            return result

    role = (user.role or '').strip().lower()
    if role in ('admin', 'super-admin', 'super_admin'):
        _room_access_cache[cache_key] = (True, now + 120)
        return True
    room_type, room_id_str = room.split(':', 1)
    room_id = int(room_id_str)
    if room_type == 'group':
        result = user.group_id == room_id
    elif room_type == 'live':
        result = True   # Any authenticated student can enter a live room
    else:
        result = False
    _room_access_cache[cache_key] = (result, now + 120)
    return result


# ────────────────────────────────────────────────────────────────────────────────
# Socket Events
# ────────────────────────────────────────────────────────────────────────────────

# sid → {user, room} for cleanup on disconnect
_sessions: dict[str, dict] = {}


@socketio.on('connect')
def on_connect():
    pass   # auth happens on 'join'


@socketio.on('join')
def on_join(data):
    token = data.get('token', '')
    room  = data.get('room', '')
    print(f'[join] received — room={room!r} token_len={len(token)}')

    if not token or not room:
        emit('error', {'msg': 'token and room are required'})
        return

    # Validate room format
    if ':' not in room or room.split(':')[0] not in ('group', 'live'):
        emit('error', {'msg': 'invalid room format. Use group:<id> or live:<id>'})
        return

    user = _get_user_from_token(token)
    if not user:
        print(f'[join] AUTH FAILED — bad/expired token')
        emit('error', {'msg': 'invalid or expired token'})
        return

    print(f'[join] auth ok — user={user.name!r} role={user.role!r} group_id={user.group_id!r}')

    if not _check_room_access(room, user):
        print(f'[join] ACCESS DENIED — user group_id={user.group_id} room={room}')
        emit('error', {'msg': 'access denied to this room'})
        return

    sid = request.sid
    join_room(room)

    # Track presence
    _presence_add(room, sid, user.id)
    _sessions[sid] = {'user': user, 'room': room}

    # Track participant list
    role = (user.role or '').strip().lower()
    is_admin = role in ('admin', 'super-admin', 'super_admin')
    _room_users.setdefault(room, {})[sid] = {
        'user_id': user.id, 'name': user.name, 'is_admin': is_admin
    }

    # Lock DB lookups out if Redis has verified the pool has been processed
    if REDIS_AVAILABLE and _is_room_loaded(room):
        history = _get_cached_messages(room)
    else:
        history = _get_cached_messages(room)
        if not history:
            history = _load_db_history(room, limit=30)
            for m in history:
                _cache_message(room, m)
            _mark_room_loaded(room) # Deflect future SQLite loading for empty rooms
            
    print(f'[join] sending history — {len(history)} msgs to {user.name}')
    emit('history', history)

    # Send muted list to the newly joined socket
    emit('muted_list', list(_muted_users.get(room, set())))

    # Broadcast updated user list and presence to room
    _broadcast_user_list(room)
    count = _presence_count(room)
    socketio.emit('presence', {'room': room, 'online_count': count}, to=room)

    # Sync watcher count to DB for live rooms so API/frontend polls show real count
    if room.startswith('live:'):
        _sync_live_watchers(int(room.split(':')[1]), count)

    print(f'[join] ✓ {user.name} joined {room}  (online: {count})')


@socketio.on('message')
def on_message(data):
    sid  = request.sid
    sess = _sessions.get(sid)
    if not sess:
        print(f'[message] REJECTED — sid {sid} not in sessions (not joined)')
        emit('error', {'msg': 'not authenticated. Send join first.'})
        return

    user      = sess['user']
    room      = data.get('room') or sess['room']
    text      = (data.get('text') or '').strip()
    image_url = data.get('image_url')

    if not text and not image_url:
        emit('error', {'msg': 'message text or image_url required'})
        return

    # Check mute
    role = (user.role or '').strip().lower()
    is_admin = role in ('admin', 'super-admin', 'super_admin')
    if not is_admin and user.id in _muted_users.get(room, set()):
        emit('error', {'msg': 'you are muted in this room', 'code': 'muted'})
        return

    # Check rate limit (admins are exempt)
    if not is_admin and not _check_rate_limit(room, user.id):
        max_msgs, window = _get_rate_limit()
        emit('error', {'msg': f'Rate limit: max {max_msgs} messages per {window}s. Please wait.', 'code': 'rate_limited'})
        return

    # Build optimistic message dict immediately (no DB round-trip on hot path)
    now_iso = datetime.utcnow().isoformat()
    optimistic_msg = {
        'id':          None,   # will be set after DB write in background
        'group_id' if room.startswith('group') else 'class_id': int(room.split(':')[1]),
        'user_id':     user.id,
        'sender_name': user.name,
        'text':        text or '',
        'image_url':   image_url,
        'created_at':  now_iso,
    }

    # 1. Cache in Redis immediately (fast path)
    _cache_message(room, optimistic_msg)

    # 2. Broadcast to all users in room instantly — no DB wait
    socketio.emit('message', optimistic_msg, to=room)

    # 3. Write to DB asynchronously in a background gevent greenlet
    #    This does NOT block the socket event loop
    def _flush_to_db():
        with app.app_context():
            try:
                real_msg = _save_message(room, user, text, image_url)
                # Optionally update Redis with the real DB id
                # (clients can reconcile on reconnect via history)
            except Exception as e:
                print(f'[message] DB write failed: {e}')

    from gevent import spawn
    spawn(_flush_to_db)


@socketio.on('admin_mute')
def on_admin_mute(data):
    """Admin-only event to mute or unmute a user in a room."""
    sid  = request.sid
    sess = _sessions.get(sid)
    if not sess:
        emit('error', {'msg': 'not authenticated'})
        return

    user = sess['user']
    role = (user.role or '').strip().lower()
    if role not in ('admin', 'super-admin', 'super_admin'):
        emit('error', {'msg': 'admin only'})
        return

    room           = data.get('room') or sess['room']
    target_user_id = int(data.get('user_id', 0))
    muted          = bool(data.get('muted', True))

    muted_set = _muted_users.setdefault(room, set())
    if muted:
        muted_set.add(target_user_id)
    else:
        muted_set.discard(target_user_id)

    # Broadcast updated mute state to everyone in room
    socketio.emit('user_muted', {'user_id': target_user_id, 'muted': muted}, to=room)
    print(f'[mute] {user.name} {"muted" if muted else "unmuted"} user_id={target_user_id} in {room}')


@socketio.on('typing')
def on_typing(data):
    sid  = request.sid
    sess = _sessions.get(sid)
    if not sess:
        return

    user      = sess['user']
    room      = data.get('room') or sess['room']
    is_typing = bool(data.get('is_typing', False))

    # Broadcast to others in the room (skip=sid so sender doesn't see own indicator)
    socketio.emit(
        'typing',
        {'user_id': user.id, 'name': user.name, 'is_typing': is_typing},
        room=room,
        skip_sid=sid,
    )


@socketio.on('leave')
def on_leave(data):
    sid  = request.sid
    sess = _sessions.get(sid)
    if not sess:
        return

    room = data.get('room') or sess['room']
    user = sess['user']
    _do_leave(sid, room, user)


@socketio.on('disconnect')
def on_disconnect():
    sid  = request.sid
    sess = _sessions.pop(sid, None)
    if sess:
        _do_leave(sid, sess['room'], sess['user'])


def _do_leave(sid: str, room: str, user):
    leave_room(room)
    _presence_remove(room, sid)
    _room_users.get(room, {}).pop(sid, None)
    _broadcast_user_list(room)
    count = _presence_count(room)
    socketio.emit('presence', {'room': room, 'online_count': count}, room=room)
    # Sync watcher count to DB for live rooms
    if room.startswith('live:'):
        _sync_live_watchers(int(room.split(':')[1]), count)
    print(f'[leave] {user.name} ← {room}  (online: {count})')


# ────────────────────────────────────────────────────────────────────────────────
# Health Check
# ────────────────────────────────────────────────────────────────────────────────
@app.route('/health')
def health():
    return {'status': 'ok', 'redis': REDIS_AVAILABLE, 'ts': time.time()}


# ────────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ────────────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    port = int(os.environ.get('CHAT_PORT', 5001))
    print(f'[chat] Starting BrighterNepal Chat Service on port {port}')
    from gevent import pywsgi
    from geventwebsocket.handler import WebSocketHandler
    server = pywsgi.WSGIServer(('0.0.0.0', port), app, handler_class=WebSocketHandler)
    server.serve_forever()
